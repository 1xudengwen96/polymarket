"""回测撮合器：合成簿 + 合成成交流，复用 PaperGateway 的队列/深度撮合逻辑。

保守化（docs/08 §3）：
- 合成簿深度 = depth_base × depth_confidence（默认 200×0.5=100 股/档）；
- 成交流由概率序列驱动：每个序列点视为一笔"穿过市场"的成交；
- maker 排队/部分成交/扫穿逻辑与实盘 PaperFillSim 完全一致。
"""

from __future__ import annotations

from decimal import Decimal

from ..execution.paper import PaperGateway
from ..features import BookState
from ..logging_setup import get_logger


class BacktestGateway(PaperGateway):
    def __init__(self, config, repo, features, depth_base: float = 200.0,
                 depth_confidence: float = 0.5, trade_size: float = 25.0) -> None:
        # features 延迟注入（回测循环里才需要 BookState）
        self._features_holder = features
        super().__init__(config, repo, features)
        self.log = get_logger("backtest.fillsim")
        self.latency_ms = 0.0  # 回测 taker 即时评估（真实延迟已在滑点/保守深度中体现）
        self.depth_base = depth_base
        self.depth_confidence = depth_confidence
        self.trade_size = trade_size
        self.last_price: dict[str, float] = {}
        self.last_series_ts: dict[str, int] = {}
        self.tick = Decimal("0.01")

    def update_price(self, token_id: str, price: float, ts_ms: int) -> None:
        """用最新概率序列点重建合成簿（tick 随价格区间变化，镜像真实规则）。"""
        self.last_price[token_id] = price
        book = self.features.books.setdefault(token_id, BookState(token_id=token_id))
        tick_f = 0.001 if price >= 0.96 or price < 0.04 else 0.01
        mid = round(price / tick_f) * tick_f
        lvl = self.depth_base * self.depth_confidence
        bids = [(f"{mid - i * tick_f:.3f}", f"{lvl:.2f}") for i in range(1, 6)]
        asks = [(f"{mid + i * tick_f:.3f}", f"{lvl:.2f}") for i in range(1, 6)]
        book.tick_size = f"{tick_f:.3f}"
        book.update_snapshot(bids, asks, None, ts_ms)

    async def feed_series_point(self, token_id: str, price: float, ts_ms: int) -> None:
        """概率序列新点 → 更新簿 + 一笔方向性合成成交（驱动 resting 撮合）。"""
        prev = self.last_price.get(token_id)
        self.last_series_ts[token_id] = ts_ms
        self.update_price(token_id, price, ts_ms)
        if prev is None:
            return
        side = "SELL" if price < prev else "BUY"
        if price == prev:
            return
        await self.on_trade(token_id, Decimal(str(round(price, 4))),
                            Decimal(str(self.trade_size)), side, ts_ms)
