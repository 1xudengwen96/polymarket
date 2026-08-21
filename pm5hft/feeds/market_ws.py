"""CLOB Market WebSocket（wss://ws-subscriptions-clob.polymarket.com/ws/market）。

事件：book / price_change / last_trade_price / tick_size_change /
      best_bid_ask / new_market / market_resolved
协议要点（docs/00 §3.3）：心跳每 10s "PING"；custom_feature_enabled=true
启用 best_bid_ask/new_market/market_resolved；支持动态 subscribe/unsubscribe。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..logging_setup import get_logger
from .base import ReconnectingWS

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]  # (event_type, msg)


class MarketWsFeed(ReconnectingWS):
    def __init__(self, on_event: EventHandler) -> None:
        super().__init__(name="market_ws", url=MARKET_WS_URL, ping_interval_s=10.0, ping_payload="PING", stall_timeout_s=45.0)
        self.on_event = on_event
        self.log = get_logger("feeds.market_ws")
        self._asset_ids: list[str] = []
        self._subscribed_ids: set[str] = set()  # 服务器当前实际订阅集
        self._did_initial_frame = False  # 本连接是否已发送初始订阅帧

    def set_asset_ids(self, asset_ids: list[str]) -> None:
        self._asset_ids = list(dict.fromkeys(str(a) for a in asset_ids))

    async def resubscribe(self) -> None:
        """增量订阅更新（与官方 SDK market_protocol.diff_state_frames 同构）。

        协议要点（经 wss://ws-subscriptions-clob.polymarket.com/ws/market 实测）：
        - 初始帧带 type=market；后续更新帧必须用 operation=subscribe/unsubscribe；
        - 重复订阅已订阅 token 会被服务器拒绝（INVALID OPERATION）→ 只发增量集合；
        - 服务器按市场粒度扇出：同一市场的两个 token 必须成对增删，
          unsubscribe 只在整个市场无订阅时生效。
        """
        if self._ws is None:
            return
        if not self._did_initial_frame:
            # 本连接未建立初始订阅（连接时注册表为空）→ 增量更新不可用，
            # 重建连接，重连时用最新 _asset_ids 发初始帧。
            await self.force_reconnect()
            return
        target = set(self._asset_ids)
        added = sorted(target - self._subscribed_ids)
        removed = sorted(self._subscribed_ids - target)
        if added:
            await self.send_json(
                {"operation": "subscribe", "assets_ids": added, "custom_feature_enabled": True}
            )
            self._subscribed_ids.update(added)
            self.log.info("market subscription updated", added=len(added))
        if removed:
            await self.send_json({"operation": "unsubscribe", "assets_ids": removed})
            self._subscribed_ids.difference_update(removed)
            self.log.info("market subscription updated", removed=len(removed))

    async def force_reconnect(self) -> None:
        """主动断开当前连接，由 ReconnectingWS 重连循环用最新订阅集重建。"""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def build_subscription(self) -> dict[str, Any] | None:
        if not self._asset_ids:
            return None
        self._subscribed_ids = set(self._asset_ids)
        self._did_initial_frame = True
        return {
            "assets_ids": self._asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }

    async def on_disconnect(self) -> None:
        # 新连接上订阅状态清零：重连后 build_subscription 重建
        self._subscribed_ids = set()
        self._did_initial_frame = False

    async def handle_message(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type")
        if not event_type:
            return
        await self.on_event(event_type, msg)
