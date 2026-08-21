"""可重连 WebSocket 基类：连接、应用层心跳、断线指数退避、重订阅钩子。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from ..logging_setup import get_logger

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class ReconnectingWS:
    """子类实现 build_subscription() 与 handle_message()。"""

    def __init__(
        self,
        name: str,
        url: str,
        ping_interval_s: float,
        ping_payload: str | None = "PING",
        reconnect_base_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        stall_timeout_s: float | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.ping_interval_s = ping_interval_s
        self.ping_payload = ping_payload  # None = 禁用应用层心跳（如 Binance 原生流）
        self.reconnect_base_s = reconnect_base_s
        self.reconnect_max_s = reconnect_max_s
        self.stall_timeout_s = stall_timeout_s  # 收包看门狗：静默超时强制重连
        self.log = get_logger(f"feeds.{name}")
        self._ws = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._ready_clear_on_close = True
        self.consecutive_failures = 0
        self._last_msg_mono = 0.0

    # ── 子类钩子 ─────────────────────────────────────────────
    async def build_subscription(self) -> dict[str, Any] | None:
        """连接建立后发送的订阅帧（可返回 None 表示无订阅）。"""
        return None

    async def handle_message(self, msg: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    async def on_disconnect(self) -> None:
        """断线回调（子类可清理状态）。"""

    async def on_connect_failure(self, consecutive: int) -> None:
        """连接失败回调（子类可轮换端点）。"""

    # ── 生命周期 ─────────────────────────────────────────────
    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def wait_ready(self, timeout_s: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout_s)
            return True
        except TimeoutError:
            return False

    async def send_json(self, obj: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(obj, separators=(",", ":")))

    async def run(self) -> None:
        backoff = self.reconnect_base_s
        while not self._stop.is_set():
            try:
                await self._connect_and_serve()
                backoff = self.reconnect_base_s
                self.consecutive_failures = 0
            except Exception as e:
                self.consecutive_failures += 1
                if self.consecutive_failures <= 3 or self.consecutive_failures % 20 == 0:
                    self.log.warning(
                        "feed connection failed",
                        name=self.name,
                        err=str(e)[:120],
                        consecutive=self.consecutive_failures,
                    )
                if self.consecutive_failures >= 5:
                    backoff = max(backoff, 15.0)
                try:
                    await self.on_connect_failure(self.consecutive_failures)
                except Exception:
                    self.log.exception("on_connect_failure failed", name=self.name)
            if self._stop.is_set():
                break
            self._ready.clear()
            self.log.warning("feed reconnecting", name=self.name, delay_s=round(backoff, 1))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max_s)

    async def _connect_and_serve(self) -> None:
        async with websockets.connect(
            self.url,
            ping_interval=None,  # 我们用自己的应用层心跳
            max_size=4 * 1024 * 1024,
            open_timeout=20,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self.log.info("feed connected", name=self.name, url=self.url)
            sub = await self.build_subscription()
            if sub is not None:
                await self.send_json(sub)
            self._ready.set()

            async def _pinger() -> None:
                if self.ping_payload is None:
                    return  # 协议级心跳由 websockets 库自动处理
                while not self._stop.is_set():
                    await asyncio.sleep(self.ping_interval_s)
                    try:
                        if self._ws is not None:
                            await self._ws.send(self.ping_payload)
                    except Exception:
                        return

            ping_task = asyncio.create_task(_pinger())

            stall_task: asyncio.Task | None = None

            async def _stall_watchdog() -> None:
                # 连接存活但无消息 → 静默停滞（RTDS 已知故障模式）→ 强制重连
                while not self._stop.is_set():
                    await asyncio.sleep(max(5.0, self.stall_timeout_s / 3))
                    if not self._last_msg_mono:
                        continue
                    gap = time.monotonic() - self._last_msg_mono
                    if gap > self.stall_timeout_s and self._ws is not None:
                        self.log.warning("feed stalled, forcing reconnect", name=self.name, gap_s=round(gap, 1))
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
                        return

            if self.stall_timeout_s is not None:
                stall_task = asyncio.create_task(_stall_watchdog())

            try:
                async for raw in ws:
                    self._last_msg_mono = time.monotonic()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    if raw in ("PONG", "pong"):
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        # 交易所会回传纯文本信息帧（如 "NO NEW ASSETS" / "SUBSCRIPTION UPDATED"）
                        self.log.debug("text frame", name=self.name, raw=str(raw)[:120])
                        continue
                    if not isinstance(msg, dict):
                        continue
                    await self.handle_message(msg)
            except ConnectionClosed as e:
                self.log.warning("feed connection closed", name=self.name, code=e.code, reason=str(e.reason)[:120])
            finally:
                ping_task.cancel()
                if stall_task is not None:
                    stall_task.cancel()
                self._ws = None
                if self._ready_clear_on_close:
                    self._ready.clear()
                try:
                    await self.on_disconnect()
                except Exception:
                    self.log.exception("on_disconnect failed", name=self.name)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
