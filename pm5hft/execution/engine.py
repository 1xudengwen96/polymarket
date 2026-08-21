"""执行引擎（docs/07）：订单生命周期、幂等、限速、截止撤单。

- client_order_id（uuid7）为幂等键；确定性 salt 使重复签名产生相同订单哈希（实盘）；
- 本地令牌桶镜像 CLOB 分桶限速（order/cancel 独立）；
- 时间守卫：t_end-10s 起拒绝新单（仅允许撤单），t_end-5s 强制全撤。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ..clock import now_ms
from ..logging_setup import get_logger


class OrderState(StrEnum):
    PENDING = "PENDING"        # 已受理，待网关确认
    LIVE = "LIVE"              # 挂单中
    PARTIAL = "PARTIAL"        # 部分成交
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"        # 提交结果未知（幂等对账中）


@dataclass
class OrderIntent:
    market_id: int
    token_id: str
    side: str            # BUY | SELL
    price: Decimal
    qty: Decimal
    tif: str             # GTC | GTD | FOK | FAK
    post_only: bool
    client_order_id: str = ""
    expires_at_ms: int | None = None   # GTD 到期
    meta: dict[str, Any] = field(default_factory=dict)  # strategy_module, token_side...

    def __post_init__(self) -> None:
        if not self.client_order_id:
            self.client_order_id = uuid7()


@dataclass
class FillEvent:
    order_id: str
    market_id: int
    token_id: str
    side: str
    price: Decimal
    qty: Decimal
    fee: Decimal
    ts_ms: int
    src: str = "paper_sim"


def uuid7() -> str:
    """uuid7：时间有序 + 随机后缀（幂等键）。"""
    ts_ms = now_ms()
    rand = uuid.uuid4().bytes[6:]
    ts_bytes = ts_ms.to_bytes(6, "big")
    b = bytearray(16)
    b[0] = (ts_bytes[0] & 0x0F) | 0x70  # version 7
    b[1:7] = ts_bytes[1:]
    b[6] = (rand[0] & 0x3F) | 0x80      # variant 10
    b[7:] = rand[1:]
    return uuid.UUID(bytes=bytes(b)).hex


def deterministic_salt(client_order_id: str) -> str:
    """确定性 salt：同一意图重复签名 → 相同订单哈希（实盘幂等）。"""
    h = hashlib.sha256(client_order_id.encode()).digest()
    return str(int.from_bytes(h[:8], "big") % (2**62))


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: float) -> None:
        self.rate = rate_per_s
        self.burst = burst
        self.tokens = burst
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_take(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class Gateway:
    """交易所网关接口（paper/live 同构，docs/07 §7）。"""

    async def submit(self, intent: OrderIntent) -> str:
        """返回网关侧订单状态（ack），异常时抛错。"""
        raise NotImplementedError

    async def cancel(self, client_order_id: str) -> bool:
        raise NotImplementedError

    async def cancel_all_market(self, market_id: int) -> int:
        raise NotImplementedError

    def clob_id_for(self, client_order_id: str) -> str | None:
        """网关侧 CLOB 订单号（诊断落库）；无则 None。"""
        return None


class ExecutionEngine:
    def __init__(
        self,
        config,
        repo,
        gateway: Gateway,
        on_fill: Callable[[FillEvent], Any] | None = None,
        deadline_for: Callable[[int], int | None] | None = None,
        on_order_closed: Callable[[str, int, str], Any] | None = None,
        now_ms_fn: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.gateway = gateway
        self.on_fill = on_fill
        self.deadline_for = deadline_for  # market_id -> t_end_ms
        self.on_order_closed = on_order_closed  # (client_order_id, market_id, state)
        self.now_ms_fn = now_ms_fn or now_ms  # 回测可注入回放时钟
        self.log = get_logger("execution")
        rl = config.e("rate_limit", {})
        self.order_bucket = TokenBucket(rl.get("order_rate_per_s", 40), rl.get("order_burst", 60))
        self.cancel_bucket = TokenBucket(rl.get("cancel_rate_per_s", 80), rl.get("cancel_burst", 120))
        self.hard_deadline_s = float(config.e("hard_deadline_before_end_s", 10))
        self.cancel_deadline_s = float(config.e("cancel_deadline_before_end_s", 5))
        self._gateway_state: dict[str, str] = {}
        self._notified_closed: deque[str] = deque(maxlen=5000)  # 已补发 on_order_closed 的订单

    # ── 提交 ─────────────────────────────────────────────────
    async def submit(self, intent: OrderIntent) -> tuple[bool, str]:
        """返回 (accepted, reason)。时间守卫 + 限速 + 幂等。"""
        deadline = self.deadline_for(intent.market_id) if self.deadline_for else None
        if deadline is not None:
            remain_ms = deadline - self.now_ms_fn()
            if remain_ms < self.hard_deadline_s * 1000:
                return False, "hard deadline: new orders blocked"
        if not self.order_bucket.try_take():
            return False, "order rate limit"
        await self.repo.upsert_order(intent, mode=self.config.settings.mode,
                                     salt=deterministic_salt(intent.client_order_id))
        try:
            state = await self.gateway.submit(intent)
            self._gateway_state[intent.client_order_id] = state
            clob_oid = self.gateway.clob_id_for(intent.client_order_id)
            await self.repo.update_order_state(intent.client_order_id, state, clob_order_id=clob_oid,
                                               reason=None)
        except Exception as e:  # noqa: BLE001
            self.log.warning("order submit failed", order=intent.client_order_id, err=str(e)[:120])
            await self.repo.update_order_state(intent.client_order_id, OrderState.REJECTED.value,
                                               reason=str(e)[:200])
            return False, f"submit failed: {str(e)[:80]}"
        return True, state

    # ── 撤单 ─────────────────────────────────────────────────
    async def cancel(self, client_order_id: str) -> bool:
        if not self.cancel_bucket.try_take():
            return False
        ok = await self.gateway.cancel(client_order_id)
        # 无论网关确认与否都落库（网关返回 False = 已不在簿口/已成交，本地视同取消），
        # 防止 CANCELLED 状态缺失导致 maintain 循环重复撤单
        await self.repo.update_order_state(
            client_order_id,
            OrderState.CANCELLED.value,
            reason=None if ok else "cancel: not active at gateway",
        )
        return ok

    async def cancel_all_market(self, market_id: int) -> int:
        orders = await self.repo.get_open_orders(market_id)
        n = 0
        for o in orders:
            if await self.cancel(o.client_order_id):
                n += 1
        return n

    # ── 成交回写 ─────────────────────────────────────────────
    async def handle_fill(self, fill: FillEvent) -> None:
        order = await self.repo.get_order(fill.order_id)
        if order is None:
            self.log.warning("fill for unknown order", order=fill.order_id)
            return
        if not fill.market_id:
            fill.market_id = order.market_id  # live 网关不知道数值 market_id，从订单回填
        if order.state in (OrderState.CANCELLED.value, OrderState.EXPIRED.value):
            self.log.warning("late fill on closed order", order=fill.order_id, state=order.state)
        await self.repo.add_fill(fill)
        new_qty, new_price, new_state = await self.repo.apply_fill(fill.order_id, fill.qty, fill.price, fill.fee)
        if new_state == OrderState.FILLED.value:
            self._gateway_state[fill.order_id] = OrderState.FILLED.value
        if self.on_fill is not None:
            try:
                result = self.on_fill(fill)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self.log.exception("on_fill callback failed", order=fill.order_id)

    # ── 周期维护（GTD 过期 / 截止撤单 / 终结单补通知）───────
    async def maintain(self) -> None:
        now = self.now_ms_fn()
        # ① 未成交即终结的单（FAK 无流动性 / FOK 深度不足 / post-only 拒绝）：
        #    网关异步改状态后不会回调 → 这里补发 on_order_closed，
        #    复位策略层的 pending 锁（tail_pending/exit_pending/mid_pending），
        #    否则出场/入场会永久卡死在该窗口
        for o in await self.repo.get_recent_terminal_orders(limit=20):
            if o.client_order_id not in self._notified_closed:
                self._notified_closed.append(o.client_order_id)
                await self._notify_closed(o, o.state)
        for o in await self.repo.get_all_open_orders():
            if o.expires_at_ms and now > o.expires_at_ms:
                await self.gateway.cancel(o.client_order_id)  # 从撮合器/交易所移除
                await self.repo.update_order_state(o.client_order_id, OrderState.EXPIRED.value,
                                                   reason="GTD expired")
                await self._notify_closed(o, OrderState.EXPIRED.value)
                continue
            deadline = self.deadline_for(o.market_id) if self.deadline_for else None
            if deadline is not None and deadline - now < self.cancel_deadline_s * 1000:
                self.log.info("deadline cancel", order=o.client_order_id, market=o.market_id)
                if await self.cancel(o.client_order_id):
                    await self._notify_closed(o, OrderState.CANCELLED.value)

    async def _notify_closed(self, order, state: str) -> None:
        if self.on_order_closed is not None:
            try:
                result = self.on_order_closed(order.client_order_id, order.market_id, state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self.log.exception("on_order_closed failed", order=order.client_order_id)
