"""Binance 公开 aggTrade 流（特征 tick 源，含攻击方向）。

aggTrade 消息: {e, E, s, a, p, q, f, l, T, m}
  m = isBuyerMaker；m=false ⇒ taker 是买方（aggressive buy）。
无应用层心跳；以 180s 静默看门狗 + 断线重连兜底。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..logging_setup import get_logger
from .base import ReconnectingWS

# stream.binance.com:9443 在部分网络被重置；data-stream.binance.vision 为官方公共数据端点
BINANCE_STREAM_URLS = [
    "wss://data-stream.binance.vision/stream",
    "wss://stream.binance.com:9443/stream",
]

TradeHandler = Callable[[str, int, float, float, bool], Awaitable[None]]
#                       symbol, ts_ms, price, qty, is_buyer_maker


class BinanceFeed(ReconnectingWS):
    def __init__(self, symbols: list[str], on_trade: TradeHandler) -> None:
        streams = "/".join(f"{s.lower()}@aggTrade" for s in symbols)
        super().__init__(
            name="binance",
            url=f"{BINANCE_STREAM_URLS[0]}?streams={streams}",
            ping_interval_s=60.0,
            ping_payload=None,  # Binance 原生流：禁止发送文本帧（会被 1008 踢掉）
            reconnect_base_s=2.0,
            reconnect_max_s=60.0,
            stall_timeout_s=180.0,
        )
        self._urls = [f"{u}?streams={streams}" for u in BINANCE_STREAM_URLS]
        self._url_idx = 0
        self.symbols = {s.lower() for s in symbols}
        self.on_trade = on_trade
        self.log = get_logger("feeds.binance")
        self._last_msg = asyncio.Event()

    async def build_subscription(self) -> dict[str, Any] | None:
        return None  # streams 参数在 URL 中

    async def on_connect_failure(self, consecutive: int) -> None:
        # 连接反复失败（地区封锁）→ 轮换备用端点
        if consecutive in (2, 6):
            self._url_idx = (self._url_idx + 1) % len(self._urls)
            self.url = self._urls[self._url_idx]
            self.log.info("binance endpoint rotated", url=self.url.split("?")[0])

    async def handle_message(self, msg: dict[str, Any]) -> None:
        self._last_msg.set()
        data = msg.get("data")  # 组合流包装
        if data is None:
            data = msg
        e = data.get("e")
        if e != "aggTrade":
            return
        symbol = str(data.get("s", "")).lower()
        if self.symbols and symbol not in self.symbols:
            return
        ts = int(data.get("T") or data.get("E") or 0)
        price = float(data.get("p", 0.0))
        qty = float(data.get("q", 0.0))
        is_buyer_maker = bool(data.get("m", False))
        await self.on_trade(symbol, ts, price, qty, is_buyer_maker)
