"""RTDS 实时参考价流（wss://ws-live-data.polymarket.com）。

- crypto_prices: Binance 参考价（~1s 更新，作为 Binance 直连失败的后备特征源）
- crypto_prices_twap_thirty / _sixty: Chainlink TWAP（**结算变量**，PTB 捕获与自结算）

协议要点（docs/00 §3.5）：
- 应用层心跳：每 5s 发送文本帧 "PING"
- 无快照/历史/重放：订阅从下一条更新开始
- TWAP filters 必须用紧凑 JSON {"symbol":"btc/usd"}；多符号则省略 filters 后自行过滤
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from ..logging_setup import get_logger
from .base import ReconnectingWS

RTDS_URL = "wss://ws-live-data.polymarket.com"

TwapHandler = Callable[[str, int, Decimal, str, int], Awaitable[None]]
#                     symbol, window_s, value, full_accuracy_value(E18), obs_ts_ms
CryptoPriceHandler = Callable[[str, int, Decimal], Awaitable[None]]
#                              symbol, obs_ts_ms, value


class RtdsFeed(ReconnectingWS):
    def __init__(
        self,
        binance_symbols: list[str],  # e.g. ["btcusdt"]
        twap_symbols: list[str],  # e.g. ["btc/usd"]
        on_crypto_price: CryptoPriceHandler,
        on_twap: TwapHandler,
    ) -> None:
        super().__init__(name="rtds", url=RTDS_URL, ping_interval_s=5.0, ping_payload="PING", stall_timeout_s=30.0)
        self.binance_symbols = binance_symbols
        self.twap_symbols = set(twap_symbols)
        self.on_crypto_price = on_crypto_price
        self.on_twap = on_twap
        self.log = get_logger("feeds.rtds")

    async def build_subscription(self) -> dict[str, Any]:
        subs = []
        # crypto_prices 实测：filters 会被忽略（无消息）；用 type "*" 收全量后客户端过滤
        if self.binance_symbols:
            subs.append({"topic": "crypto_prices", "type": "*"})
        # 多符号 TWAP：省略 filters，收全量后按 payload.symbol 过滤（官方文档指引）
        subs.append({"topic": "crypto_prices_twap_thirty", "type": "update"})
        subs.append({"topic": "crypto_prices_twap_sixty", "type": "update"})
        return {"action": "subscribe", "subscriptions": subs}

    async def handle_message(self, msg: dict[str, Any]) -> None:
        topic = msg.get("topic")
        payload = msg.get("payload") or {}
        if topic == "crypto_prices":
            symbol = str(payload.get("symbol", ""))
            if self.binance_symbols and symbol not in self.binance_symbols:
                return
            ts = int(payload.get("timestamp") or 0)
            value = Decimal(str(payload.get("value", "0")))
            await self.on_crypto_price(symbol, ts, value)
        elif topic in ("crypto_prices_twap_thirty", "crypto_prices_twap_sixty"):
            symbol = str(payload.get("symbol", ""))
            if self.twap_symbols and symbol not in self.twap_symbols:
                return
            window_s = int(payload.get("window_s") or 0)
            full = str(payload.get("full_accuracy_value") or "")
            obs_ts = int(payload.get("timestamp") or 0)
            value = Decimal(str(payload.get("value") or "0"))
            if window_s == 0:
                # 兼容 SDK 命名（windowSeconds）
                window_s = int(payload.get("windowSeconds") or 0)
                obs_ts = int(payload.get("timestamp") or 0)
            await self.on_twap(symbol, window_s, value, full, obs_ts)
