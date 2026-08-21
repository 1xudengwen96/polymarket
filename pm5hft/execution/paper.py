"""Paper 撮合（docs/09）：基于真实簿口的保守模拟。

铁律：绝不"看到 ask=43¢ 就默认成交"。
- Maker（post-only）：我方排在真实队列尾部；仅当真实成交流击穿我方价位时成交；
  同价位按"队首优先"：真实簿口在我方之前的量先被消耗，剩余才轮到我们（部分成交）。
- Taker（FOK/FAK）：延迟 latency_ms 后在"决策时刻"的真实簿上按限价撮合；
  FOK 深度不足则整单不成；FAK 部分成交。
- 手续费：maker 免费（按配置返佣暂记 0），taker 按配置 bps。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..clock import now_ms
from ..logging_setup import get_logger
from ..persistence import Repo
from .engine import FillEvent, OrderIntent, OrderState


@dataclass
class RestingOrder:
    intent: OrderIntent
    filled: Decimal = Decimal("0")
    ahead: Decimal = Decimal("0")  # 我方价位上排在前面的真实数量
    alive: bool = True
    created_ms: int = 0


class PaperGateway:
    """ExchangeGateway 的 paper 实现。fill_handler 由 ExecutionEngine 注入。"""

    def __init__(self, config, repo: Repo, features) -> None:
        self.config = config
        self.repo = repo
        self.features = features
        self.log = get_logger("execution.paper")
        self.latency_ms = float(config.e("latency_ms", 250))
        self.taker_fee = Decimal(str(config.s("fees.taker_fee_bps", 1.0))) / Decimal("10000")
        self.fill_handler: Callable[[FillEvent], Any] | None = None
        self._resting: dict[str, RestingOrder] = {}
        self._taker_pending: dict[str, bool] = {}  # client_order_id -> cancelled
        self._tasks: set[asyncio.Task] = set()

    # ── 真实成交流 → 撮合 resting ───────────────────────────
    async def on_trade(self, token_id: str, price: Decimal, qty: Decimal, side: str, ts_ms: int) -> None:
        for oid, r in list(self._resting.items()):
            if not r.alive or r.intent.token_id != token_id:
                continue
            fill_qty = self._match(r, price, qty, side)
            if fill_qty <= 0:
                continue
            r.filled += fill_qty
            if r.filled >= r.intent.qty:
                r.alive = False
            # maker 成交价 = 我方限价（扫穿时也按我方挂单价成交）
            await self._emit_fill(r.intent, r.intent.price, fill_qty, ts_ms, src="paper_sim")
            if not r.alive:
                self._resting.pop(oid, None)
            else:
                await self.repo.update_order_state(oid, OrderState.PARTIAL.value, reason=None)

    @staticmethod
    def _match(r: RestingOrder, trade_price: Decimal, trade_qty: Decimal, taker_side: str) -> Decimal:
        """返回本次成交数量（0 = 未触及）。"""
        p = r.intent.price
        rem = r.intent.qty - r.filled
        if r.intent.side == "BUY":
            # 攻击性卖方（taker_side=SELL）击穿买价
            if taker_side != "SELL":
                return Decimal("0")
            if trade_price == p:
                take = min(rem, max(Decimal("0"), trade_qty - r.ahead))
                r.ahead = max(Decimal("0"), r.ahead - trade_qty)
                return take
            if trade_price < p:
                return rem  # 扫穿我方价位 → 全数成交
            return Decimal("0")
        # SELL：攻击性买方抬升卖价
        if taker_side != "BUY":
            return Decimal("0")
        if trade_price == p:
            take = min(rem, max(Decimal("0"), trade_qty - r.ahead))
            r.ahead = max(Decimal("0"), r.ahead - trade_qty)
            return take
        if trade_price > p:
            return rem
        return Decimal("0")

    async def _emit_fill(self, intent: OrderIntent, price: Decimal, qty: Decimal, ts_ms: int,
                         src: str = "paper_sim") -> None:
        fee = Decimal("0")
        if intent.tif in ("FOK", "FAK"):
            fee = (price * qty * self.taker_fee).quantize(Decimal("0.0001"))
        fill = FillEvent(
            order_id=intent.client_order_id, market_id=intent.market_id,
            token_id=intent.token_id, side=intent.side, price=price, qty=qty,
            fee=fee, ts_ms=ts_ms, src=src,
        )
        if self.fill_handler is not None:
            try:
                result = self.fill_handler(fill)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self.log.exception("fill handler failed", order=intent.client_order_id)

    # ── Gateway 接口 ─────────────────────────────────────────
    async def submit(self, intent: OrderIntent) -> str:
        book = self.features.books.get(intent.token_id)
        # 有效 tick：价格区间规则（≥0.96 / <0.04 → 0.001）
        if intent.price >= Decimal("0.96") or intent.price < Decimal("0.04"):
            tick = Decimal("0.001")
        else:
            tick = Decimal(book.tick_size if book else "0.01")
        if intent.price % tick != 0:
            await self.repo.update_order_state(intent.client_order_id, OrderState.REJECTED.value,
                                               reason="tick size violation")
            return OrderState.REJECTED.value
        if intent.qty <= 0:
            await self.repo.update_order_state(intent.client_order_id, OrderState.REJECTED.value,
                                               reason="invalid qty")
            return OrderState.REJECTED.value
        if intent.tif in ("FOK", "FAK"):
            self._schedule_taker(intent)
            return OrderState.PENDING.value
        # maker：post-only 校验（不可穿越簿口）
        if intent.post_only and book is not None:
            if intent.side == "BUY" and book.best_ask is not None and intent.price >= Decimal(str(book.best_ask)):
                await self.repo.update_order_state(intent.client_order_id, OrderState.REJECTED.value,
                                                   reason="post-only would cross")
                return OrderState.REJECTED.value
            if intent.side == "SELL" and book.best_bid is not None and intent.price <= Decimal(str(book.best_bid)):
                await self.repo.update_order_state(intent.client_order_id, OrderState.REJECTED.value,
                                                   reason="post-only would cross")
                return OrderState.REJECTED.value
        # 队首位置：挂出时我方价位上的真实存量 = 排在我们前面的量
        ahead = Decimal("0")
        if book is not None:
            lvl = book.levels.get(float(intent.price))
            if lvl is not None:
                ahead = Decimal(str(abs(lvl)))
        self._resting[intent.client_order_id] = RestingOrder(
            intent=intent, ahead=ahead, created_ms=now_ms(),
        )
        return OrderState.LIVE.value

    def _schedule_taker(self, intent: OrderIntent) -> None:
        self._taker_pending[intent.client_order_id] = False
        task = asyncio.get_running_loop().create_task(self._taker_eval(intent))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _taker_eval(self, intent: OrderIntent) -> None:
        await asyncio.sleep(self.latency_ms / 1000.0)
        cancelled = self._taker_pending.pop(intent.client_order_id, False)
        if cancelled:
            return  # 已撤销（repo 状态由执行引擎置为 CANCELLED）
        book = self.features.books.get(intent.token_id)
        if book is None:
            await self.repo.update_order_state(intent.client_order_id, OrderState.EXPIRED.value,
                                               reason="no book")
            return
        if intent.side == "BUY":
            levels = sorted(((p, s) for p, s in book.levels.items() if s < 0 and p <= float(intent.price)),
                            key=lambda x: x[0])  # asks ≤ limit，升序
            levels = [(p, -s) for p, s in levels]
        else:
            levels = sorted(((p, s) for p, s in book.levels.items() if s > 0 and p >= float(intent.price)),
                            key=lambda x: -x[0])  # bids ≥ limit，降序
        avail = Decimal("0")
        consumed: list[tuple[Decimal, Decimal]] = []
        for p, s in levels:
            take = min(Decimal(str(s)), intent.qty - avail)
            if take <= 0:
                continue
            consumed.append((Decimal(str(p)), take))
            avail += take
            if avail >= intent.qty:
                break
        if intent.tif == "FOK" and avail < intent.qty:
            await self.repo.update_order_state(intent.client_order_id, OrderState.EXPIRED.value,
                                               reason="FOK: insufficient depth")
            return
        if avail <= 0:
            await self.repo.update_order_state(intent.client_order_id, OrderState.EXPIRED.value,
                                               reason="no liquidity at limit")
            return
        total_cost = sum(p * q for p, q in consumed)
        avg = (total_cost / avail).quantize(Decimal("0.0001"))
        await self._emit_fill(intent, avg, avail, now_ms(), src="paper_taker")
        if avail < intent.qty:
            # FAK 语义：能成交多少算多少，剩余部分取消
            await self.repo.update_order_state(intent.client_order_id, OrderState.CANCELLED.value,
                                               reason="FAK partial fill remainder cancelled")

    async def cancel(self, client_order_id: str) -> bool:
        r = self._resting.pop(client_order_id, None)
        if r is not None:
            return True
        if client_order_id in self._taker_pending:
            self._taker_pending[client_order_id] = True
            return True
        return False

    async def cancel_all_market(self, market_id: int) -> int:
        doomed = [oid for oid, r in self._resting.items() if r.intent.market_id == market_id]
        for oid in doomed:
            self._resting.pop(oid, None)
        return len(doomed)

    def clob_id_for(self, client_order_id: str) -> str | None:
        return None

    def resting_summary(self) -> dict[str, Any]:
        return {
            oid: {
                "side": r.intent.side,
                "price": str(r.intent.price),
                "qty": str(r.intent.qty),
                "filled": str(r.filled),
                "ahead": str(r.ahead),
            }
            for oid, r in self._resting.items()
        }
