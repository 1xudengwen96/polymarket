"""持久化仓储：低频直写 + 高频缓冲批量写。"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from ..db import session_factory
from ..logging_setup import get_logger
from ..models import BookSnapshot, Market, MarketStatus, Settlement, Tick, Trade, TwapSample

OPEN_ORDER_STATES = ("PENDING", "LIVE", "PARTIAL")
OPEN_ORDER_STATES_CI = tuple(s.lower() for s in OPEN_ORDER_STATES)


class Repo:
    def __init__(self) -> None:
        self.log = get_logger("persistence")
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=200_000)
        self._flush_task: asyncio.Task | None = None
        self._flushed = 0
        self._dropped = 0

    # ── 低频直写 ─────────────────────────────────────────────
    async def get_runtime_settings(self) -> dict[str, str]:
        from sqlalchemy import select

        from ..models import RuntimeSetting

        async with session_factory()() as sess:
            rows = await sess.execute(select(RuntimeSetting.setting_key, RuntimeSetting.value))
            return dict(rows.all())

    async def set_runtime_setting(self, key: str, value: str) -> None:
        """upsert 一条 runtime_settings（bot 侧写回，如 rest_reason 供 Dashboard 展示）。"""
        from ..clock import now_ms
        from ..models import RuntimeSetting

        async with session_factory()() as sess:
            row = await sess.get(RuntimeSetting, key)
            if row is None:
                sess.add(RuntimeSetting(setting_key=key, value=value, updated_ts_ms=now_ms()))
            else:
                row.value = value
                row.updated_ts_ms = now_ms()
            await sess.commit()

    async def upsert_market(self, rec) -> None:
        async with session_factory()() as sess:
            m = await sess.get(Market, rec.market_id)
            if m is None:
                m = Market(id=rec.market_id)
                sess.add(m)
            m.event_id = rec.event_id
            m.series_slug = ""
            m.slug = rec.slug
            m.asset = rec.asset
            m.duration_s = rec.t_end - rec.t_start
            m.t_start = rec.t_start
            m.t_end = rec.t_end
            m.condition_id = rec.condition_id
            m.token_up = rec.token_up
            m.token_down = rec.token_down
            m.question = rec.question
            m.resolution_source = rec.resolution_source
            m.twap_lookback_s = rec.twap_lookback_s
            m.tick_size = rec.tick_size
            m.min_order_size = rec.min_order_size
            m.neg_risk = rec.neg_risk
            m.fee_schedule = json.dumps(rec.fee_schedule) if rec.fee_schedule else None
            await sess.commit()

    async def get_market(self, asset: str, t_start: int):
        from sqlalchemy import select

        async with session_factory()() as sess:
            res = await sess.execute(select(Market).where(Market.asset == asset, Market.t_start == t_start))
            return res.scalar_one_or_none()

    async def get_market_by_id(self, market_id: int):
        async with session_factory()() as sess:
            return await sess.get(Market, market_id)

    async def get_recent_ticks(self, asset: str, since_ms: int) -> list[tuple[int, float]]:
        """最近 N 秒 spot tick（重启回填 tick 缓冲 → PTB 重建兜底）。

        按 ts 去重（重启可能重复持久化同秒 bar），保留每毫秒最后一条。
        """
        from sqlalchemy import select

        async with session_factory()() as sess:
            res = await sess.execute(
                select(Tick.ts_ms, Tick.price)
                .where(Tick.asset == asset, Tick.ts_ms >= since_ms)
                .order_by(Tick.ts_ms)
            )
            dedup: dict[int, float] = {}
            for ts, p in res.all():
                dedup[ts] = float(p)
            return sorted(dedup.items())

    async def add_market_status(
        self,
        market_id: int,
        state: str,
        accepting_orders: bool | None = None,
        gamma_closed: bool | None = None,
        gamma_outcome_prices: list[str] | None = None,
        detail: str | None = None,
    ) -> None:
        from ..clock import now_ms

        async with session_factory()() as sess:
            sess.add(
                MarketStatus(
                    market_id=market_id,
                    ts_ms=now_ms(),
                    state=state,
                    accepting_orders=accepting_orders,
                    gamma_closed=gamma_closed,
                    gamma_outcome_prices=json.dumps(gamma_outcome_prices) if gamma_outcome_prices else None,
                    detail=detail,
                )
            )
            await sess.commit()

    async def save_settlement(
        self,
        market_id: int,
        ptb_e18: str | None,
        final_e18: str | None,
        ptb_obs_ts_ms: int | None,
        final_obs_ts_ms: int | None,
        self_result: str | None,
        self_settled_at_ms: int | None,
        ptb_src: str | None,
    ) -> None:
        async with session_factory()() as sess:
            s = await sess.get(Settlement, market_id)
            if s is None:
                s = Settlement(market_id=market_id)
                sess.add(s)
            s.ptb_e18 = ptb_e18
            s.final_e18 = final_e18
            s.ptb_obs_ts_ms = ptb_obs_ts_ms
            s.final_obs_ts_ms = final_obs_ts_ms
            s.self_result = self_result
            s.self_settled_at_ms = self_settled_at_ms
            s.ptb_src = ptb_src
            await sess.commit()

    async def mark_settlement_reconciled(
        self,
        market_id: int,
        gamma_result: str,
        gamma_prices: str,
        dispute: str | None,
    ) -> None:
        async with session_factory()() as sess:
            s = await sess.get(Settlement, market_id)
            if s is None:
                return
            s.gamma_result = gamma_result
            s.gamma_prices = gamma_prices
            s.reconciled = True
            s.dispute = dispute
            await sess.commit()

    async def get_settlement(self, market_id: int) -> Settlement | None:
        async with session_factory()() as sess:
            return await sess.get(Settlement, market_id)

    async def get_open_positions(self, market_id: int) -> list:
        from sqlalchemy import select

        from ..models import Position

        async with session_factory()() as sess:
            res = await sess.execute(
                select(Position).where(Position.market_id == market_id, Position.state == "OPEN")
            )
            return list(res.scalars().all())

    async def get_unreconciled_settlements(self, max_age_s: int = 3600) -> list[Settlement]:
        from sqlalchemy import select

        from ..clock import now_ms

        cutoff = now_ms() - max_age_s * 1000
        async with session_factory()() as sess:
            res = await sess.execute(
                select(Settlement).where(
                    Settlement.reconciled.is_(False),
                    Settlement.self_settled_at_ms.is_not(None),
                    Settlement.self_settled_at_ms >= cutoff,
                )
            )
            return list(res.scalars().all())

    # ── 决策日志 ─────────────────────────────────────────────
    async def add_decision(self, rec, wt, f: dict[str, Any], decision, pos, cal_p, market_p, edge) -> None:
        from ..clock import now_ms
        from ..models import DecisionLog

        def s(v, nd=8):
            if v is None:
                return None
            if isinstance(v, float):
                return format(v, f".{nd}f")
            return str(v)

        row = DecisionLog(
            ts_ms=now_ms(),
            market_id=rec.market_id,
            asset=rec.asset,
            ref_price=s(wt.ptb) if wt is not None else None,
            btc_price=s(f.get("btc_price"), 4),
            twap_now=s(f.get("twap_now"), 4),
            remaining_s=f.get("remaining_s"),
            into_window_s=f.get("into_window_s"),
            up_bid=s(f.get("up_bid"), 2),
            up_ask=s(f.get("up_ask"), 2),
            down_bid=s(f.get("down_bid"), 2),
            down_ask=s(f.get("down_ask"), 2),
            fair_prob=s(cal_p, 6),
            cal_prob=s(cal_p, 6),
            market_prob=s(market_p, 6),
            gross_edge=s(edge.gross_edge if edge else None, 6),
            net_edge=s(edge.net_edge if edge else None, 6),
            norm_distance=s(f.get("dist_bps"), 4),
            vol_10s=s(f.get("rv_5s"), 8),
            vol_60s=s(f.get("rv_60s"), 8),
            momentum=s(f.get("ret_5s"), 8),
            obi=s(f.get("obi10"), 6),
            tfi=s(f.get("tfi_5s"), 6),
            agg_buy=s(f.get("agg_buy_5s"), 4),
            agg_sell=s(f.get("agg_sell_5s"), 4),
            reversal_score=s(f.get("reversal_score"), 6),
            pos_state=pos.state if pos is not None else None,
            pos_up_qty=s(pos.up_qty, 4) if pos is not None else None,
            pos_down_qty=s(pos.down_qty, 4) if pos is not None else None,
            avg_entry_up=s(pos.avg_up, 4) if pos is not None else None,
            avg_entry_down=s(pos.avg_down, 4) if pos is not None else None,
            decision=decision.action,
            reject_reason=decision.reason,
            extra=json.dumps({k: (f"{v:.6f}" if isinstance(v, float) else v) for k, v in f.items()}),
        )
        async with session_factory()() as sess:
            sess.add(row)
            await sess.commit()

    # ── 订单/成交/持仓 ───────────────────────────────────────
    async def upsert_order(self, intent, mode: str, salt: str):
        from ..clock import now_ms
        from ..models import Order

        async with session_factory()() as sess:
            o = await sess.get(Order, intent.client_order_id)
            if o is None:
                o = Order(client_order_id=intent.client_order_id, mode=mode)
                sess.add(o)
            o.market_id = intent.market_id
            o.token_id = intent.token_id
            o.side = intent.side
            o.price = str(intent.price)
            o.size = str(intent.qty)
            o.tif = intent.tif
            o.post_only = intent.post_only
            o.state = "PENDING"
            o.filled_qty = "0"
            o.avg_fill_price = None
            o.created_ts_ms = o.created_ts_ms or now_ms()
            o.updated_ts_ms = now_ms()
            o.expires_at_ms = intent.expires_at_ms
            o.salt = salt
            o.meta = json.dumps(intent.meta) if intent.meta else None
            await sess.commit()
            await sess.refresh(o)
            return o

    async def update_order_state(self, client_order_id: str, state: str | None,
                                 clob_order_id: str | None = None, reason: str | None = None) -> None:
        from ..clock import now_ms
        from ..models import Order

        async with session_factory()() as sess:
            o = await sess.get(Order, client_order_id)
            if o is None:
                return
            if state is not None:
                o.state = state
            if clob_order_id is not None:
                o.clob_order_id = clob_order_id
            if reason is not None:
                o.reason = reason
            o.updated_ts_ms = now_ms()
            await sess.commit()

    async def get_order(self, client_order_id: str):
        from ..models import Order

        async with session_factory()() as sess:
            return await sess.get(Order, client_order_id)

    async def get_open_orders(self, market_id: int) -> list:
        from sqlalchemy import func, select

        from ..models import Order

        async with session_factory()() as sess:
            res = await sess.execute(
                select(Order).where(
                    Order.market_id == market_id,
                    # 大小写不敏感：live 网关曾写入小写 "live"（历史行），
                    # 严格大写匹配会让僵尸挂单永久停留 open 状态
                    func.lower(Order.state).in_(OPEN_ORDER_STATES_CI),
                )
            )
            return list(res.scalars().all())

    async def get_all_open_orders(self) -> list:
        from sqlalchemy import func, select

        from ..models import Order

        async with session_factory()() as sess:
            res = await sess.execute(
                select(Order).where(func.lower(Order.state).in_(OPEN_ORDER_STATES_CI))
            )
            return list(res.scalars().all())

    async def get_recent_terminal_orders(self, limit: int = 20) -> list:
        """最近未成交即终结的订单（EXPIRED/CANCELLED/REJECTED）→ 补发 on_order_closed 用。"""
        from sqlalchemy import select

        from ..models import Order

        async with session_factory()() as sess:
            res = await sess.execute(
                select(Order)
                .where(Order.state.in_(("EXPIRED", "CANCELLED", "REJECTED")))
                .order_by(Order.created_ts_ms.desc())
                .limit(limit)
            )
            return list(res.scalars().all())

    async def add_fill(self, fill) -> None:
        from ..models import Fill

        async with session_factory()() as sess:
            sess.add(Fill(
                order_id=fill.order_id,
                market_id=fill.market_id,
                token_id=fill.token_id,
                side=fill.side,
                price=str(fill.price),
                qty=str(fill.qty),
                fee=str(fill.fee),
                ts_ms=fill.ts_ms,
                src=fill.src,
            ))
            await sess.commit()

    async def apply_fill(self, order_id: str, qty, price, fee=Decimal("0")) -> tuple[str, str, str]:
        """更新订单 + 持仓行；返回 (filled_qty, avg_price, new_state)。"""
        from decimal import Decimal

        from ..clock import now_ms
        from ..models import Order, Position

        q = Decimal(str(qty))
        p = Decimal(str(price))
        async with session_factory()() as sess:
            o = await sess.get(Order, order_id)
            if o is None:
                return "0", str(p), "FILLED"
            filled = Decimal(o.filled_qty or "0")
            avg = Decimal(o.avg_fill_price) if o.avg_fill_price else Decimal("0")
            new_avg = (avg * filled + p * q) / (filled + q) if filled + q > 0 else p
            filled += q
            o.filled_qty = str(filled)
            o.avg_fill_price = str(new_avg)
            o.updated_ts_ms = now_ms()
            if filled >= Decimal(o.size or "0"):
                o.state = "FILLED"
            else:
                o.state = "PARTIAL"

            # 持仓行
            pos = await sess.get(Position, {"market_id": o.market_id, "token_id": o.token_id})
            if pos is None:
                pos = Position(market_id=o.market_id, token_id=o.token_id,
                               qty="0", avg_entry=None, realized_pnl="0", fees="0",
                               state="OPEN", opened_ts_ms=now_ms())
                sess.add(pos)
            pos_qty = Decimal(pos.qty or "0")
            pos_avg = Decimal(pos.avg_entry) if pos.avg_entry else Decimal("0")
            realized = Decimal(pos.realized_pnl or "0")
            fees = Decimal(pos.fees or "0")
            fill_fee = Decimal(str(fee)) if fee is not None else Decimal("0")
            if o.side == "BUY":
                new_qty = pos_qty + q
                pos.avg_entry = str((pos_avg * pos_qty + p * q) / new_qty) if new_qty > 0 else None
                pos.qty = str(new_qty)
            else:
                sold = min(q, pos_qty)
                realized += (p - pos_avg) * sold
                new_qty = pos_qty - sold
                pos.qty = str(new_qty)
                pos.avg_entry = str(pos_avg) if new_qty > 0 else None
            pos.realized_pnl = str(realized)
            pos.fees = str(fees + fill_fee)
            if pos.qty == "0" and realized != 0:
                pos.state = "CLOSED"
            await sess.commit()
            return str(filled), str(new_avg), o.state

    async def settle_positions(self, market_id: int, winning_side: str) -> str:
        """结算：胜方 payout=1，败方=0；返回该市场总已实现 PnL（含费用）。"""
        from decimal import Decimal

        from sqlalchemy import select

        from ..clock import now_ms
        from ..models import Market, Position

        total = Decimal("0")
        async with session_factory()() as sess:
            m = await sess.get(Market, market_id)
            if m is None:
                return "0"
            rows = list((await sess.execute(
                select(Position).where(Position.market_id == market_id)
            )).scalars().all())
            for pos in rows:
                side = "UP" if pos.token_id == m.token_up else "DOWN"
                qty = Decimal(pos.qty or "0")
                avg = Decimal(pos.avg_entry) if pos.avg_entry else Decimal("0")
                payout = Decimal("1") if side == winning_side else Decimal("0")
                realized = Decimal(pos.realized_pnl or "0")
                fees = Decimal(pos.fees or "0")
                realized += qty * (payout - avg)
                pos.realized_pnl = str(realized - fees)  # 最终 PnL 扣除全部费用
                if qty > 0:
                    # 结算兑付后持仓清零；只有真正持有到结算的仓位才标记 SETTLED
                    pos.qty = "0"
                    pos.avg_entry = None
                    pos.state = "SETTLED"
                    pos.settled_result = "WIN" if payout == 1 else "LOSS"
                    pos.settled_ts_ms = now_ms()
                total += realized - fees
            await sess.commit()
            return str(total)

    # ── 高频缓冲 ─────────────────────────────────────────────
    def submit(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                self.log.warning("persistence queue full, dropping", dropped=self._dropped)

    async def insert_equity_snapshot(self, equity: str, daily: str, hourly: str, drawdown: str,
                                     mode: str = "paper") -> None:
        from ..clock import now_ms
        from ..models import EquitySnapshot

        async with session_factory()() as sess:
            sess.add(EquitySnapshot(ts_ms=now_ms(), mode=mode, equity=equity,
                                    daily_pnl=daily, hourly_pnl=hourly, drawdown=drawdown))
            await sess.commit()

    async def insert_twap_sample(self, symbol: str, window_s: int, value_e18: str, obs_ts_ms: int, src: str) -> None:
        from ..clock import now_ms

        self.submit("twap", {
            "ts_ms": now_ms(), "symbol": symbol, "window_s": window_s,
            "value_e18": value_e18, "obs_ts_ms": obs_ts_ms, "src": src,
        })

    async def insert_tick(self, asset: str, ts_ms: int, price: str, vol_1s: str | None,
                          agg_buy_1s: str | None, agg_sell_1s: str | None, n_trades_1s: int | None) -> None:
        self.submit("tick", {
            "ts_ms": ts_ms, "asset": asset, "price": price, "vol_1s": vol_1s,
            "agg_buy_1s": agg_buy_1s, "agg_sell_1s": agg_sell_1s, "n_trades_1s": n_trades_1s,
        })

    async def insert_trade(self, market_id: int | None, token_id: str, side: str, price: str, size: str) -> None:
        from ..clock import now_ms

        self.submit("trade", {
            "ts_ms": now_ms(), "market_id": market_id, "token_id": token_id,
            "side": side, "price": price, "size": size,
        })

    async def insert_book_snapshot(self, token_id: str, book_hash: str | None, best_bid: str | None,
                                   best_ask: str | None, bid10: str | None, ask10: str | None,
                                   tick_size: str | None) -> None:
        from ..clock import now_ms

        self.submit("book", {
            "ts_ms": now_ms(), "token_id": token_id, "book_hash": book_hash,
            "best_bid": best_bid, "best_ask": best_ask, "bid10": bid10, "ask10": ask10,
            "tick_size": tick_size,
        })

    # ── flush 任务 ───────────────────────────────────────────
    async def flush_loop(self, interval_s: float = 1.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self._flush()

    async def _flush(self) -> None:
        batch: list[tuple[str, Any]] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            if len(batch) >= 5000:
                break
        if not batch:
            return
        for attempt in range(2):
            try:
                async with session_factory()() as sess:
                    for kind, p in batch:
                        if kind == "twap":
                            sess.add(TwapSample(**p))
                        elif kind == "tick":
                            sess.add(Tick(**p))
                        elif kind == "trade":
                            sess.add(Trade(**p))
                        elif kind == "book":
                            sess.add(BookSnapshot(**p))
                    await sess.commit()
                self._flushed += len(batch)
                return
            except Exception:
                if attempt == 0:
                    self.log.warning("flush locked, retrying", n=len(batch))
                    await asyncio.sleep(2.0)
                    continue
                self.log.exception("flush failed", n=len(batch))
                # 重试仍失败：丢弃批次（避免阻塞内存；数据损失记日志）
                self._dropped += len(batch)
