"""Feed 订阅帧与注册表清理测试。"""

import json
from types import SimpleNamespace

from pm5hft.config import Config
from pm5hft.feeds.market_ws import MarketWsFeed
from pm5hft.market_registry import MarketRegistry


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, s: str) -> None:
        self.sent.append(s)

    async def close(self) -> None:
        self.closed = True


async def test_resubscribe_sends_operation_subscribe_for_added_only():
    """订阅更新必须用 operation=subscribe 且只发增量（重复订阅被服务器拒绝
    INVALID OPERATION，初始帧 type=market 不可复用为更新帧）。"""
    feed = MarketWsFeed(on_event=lambda *a: None)
    ws = FakeWS()
    feed._ws = ws  # noqa: SLF001
    # 模拟连接已建立：初始帧订阅 t1,t2
    feed.set_asset_ids(["t1", "t2"])
    initial = await feed.build_subscription()
    assert initial["type"] == "market"
    # 轮换：新增 t3,t4
    feed.set_asset_ids(["t1", "t2", "t3", "t4"])
    await feed.resubscribe()
    assert len(ws.sent) == 1
    msg = json.loads(ws.sent[0])
    assert msg["operation"] == "subscribe"
    assert msg["assets_ids"] == ["t3", "t4"]
    assert msg["custom_feature_enabled"] is True


async def test_resubscribe_sends_operation_unsubscribe_for_removed():
    feed = MarketWsFeed(on_event=lambda *a: None)
    ws = FakeWS()
    feed._ws = ws  # noqa: SLF001
    feed.set_asset_ids(["t1", "t2", "t3", "t4"])
    await feed.build_subscription()
    # 过期窗口清理：只剩 t1,t2
    feed.set_asset_ids(["t1", "t2"])
    await feed.resubscribe()
    assert len(ws.sent) == 1
    msg = json.loads(ws.sent[0])
    assert msg["operation"] == "unsubscribe"
    assert msg["assets_ids"] == ["t3", "t4"]
    assert "custom_feature_enabled" not in msg


async def test_resubscribe_no_diff_sends_nothing():
    feed = MarketWsFeed(on_event=lambda *a: None)
    ws = FakeWS()
    feed._ws = ws  # noqa: SLF001
    feed.set_asset_ids(["t1", "t2"])
    await feed.build_subscription()
    await feed.resubscribe()  # 无变化 → 空操作（60s 订阅保险触发）
    assert ws.sent == []


async def test_resubscribe_without_initial_frame_forces_reconnect():
    """连接建立时注册表为空（未发初始帧）→ 增量更新不可用，重建连接。"""
    feed = MarketWsFeed(on_event=lambda *a: None)
    ws = FakeWS()
    feed._ws = ws  # noqa: SLF001
    feed.set_asset_ids(["t1", "t2"])
    await feed.resubscribe()
    assert ws.closed is True
    assert ws.sent == []


def test_registry_prune_removes_expired_windows():
    """过期窗口清理：订阅列表保持精简（防交易所订阅帧截断尾部 token）。"""
    reg = MarketRegistry(Config(), None)
    reg._markets[("btc", 100)] = SimpleNamespace(t_end=1000, token_up="u1", token_down="d1")  # noqa: SLF001
    reg._markets[("eth", 200)] = SimpleNamespace(t_end=3000, token_up="u2", token_down="d2")  # noqa: SLF001
    n = reg.prune(1500)
    assert n == 1
    assert ("btc", 100) not in reg._markets
    assert ("eth", 200) in reg._markets
    assert reg.subscription_tokens() == ["u2", "d2"]


def test_rest_book_snapshot_replaces_levels_and_bests():
    """REST /books 快照：levels 全量替换、最优价重算、新鲜度用到达时刻。

    REST 返回格式：bids 升序、asks 降序（docs/00 实测）——BookState 需自行排序。
    """
    from pm5hft.features import FeatureStore

    fs = FeatureStore(Config())
    # REST 原样：bids 升序、asks 降序
    fs.apply_rest_book(
        "T1",
        bids=[("0.45", "10"), ("0.46", "5"), ("0.47", "20")],
        asks=[("0.54", "8"), ("0.53", "12"), ("0.52", "3")],
    )
    book = fs.books["T1"]
    assert book.best_bid == 0.47  # 排序后取最高买
    assert book.best_ask == 0.52  # 排序后取最低卖
    assert len(book.levels) == 6
    # 再次快照替换（旧档位应消失）
    fs.apply_rest_book(
        "T1",
        bids=[("0.40", "7")],
        asks=[("0.60", "9")],
    )
    assert book.best_bid == 0.40
    assert book.best_ask == 0.60
    assert len(book.levels) == 2
    # 新鲜度已更新（到达时刻）
    assert fs.book_age_ms("T1") is not None and fs.book_age_ms("T1") < 5000


def test_rest_tokens_skip_stale_stream_updates():
    """REST 为主数据源时：WS 流（过期 10-20s）不得覆盖 REST 新鲜快照。"""
    import asyncio

    from pm5hft.features import FeatureStore

    fs = FeatureStore(Config())
    fs.apply_rest_book("T1", bids=[("0.40", "7")], asks=[("0.60", "9")])
    fs.set_rest_tokens(["T1"])
    # 过期流消息到达 → 被跳过，簿口保持 REST 值
    asyncio.run(fs.on_price_change({
        "timestamp": "1000",
        "price_changes": [{"asset_id": "T1", "price": "0.30", "size": "5", "side": "BUY",
                           "best_bid": "0.30", "best_ask": "0.50"}],
    }))
    book = fs.books["T1"]
    assert book.best_bid == 0.40 and book.best_ask == 0.60
    # 回退（REST 失败）→ 流恢复更新
    fs.clear_rest_tokens()
    asyncio.run(fs.on_price_change({
        "timestamp": "2000",
        "price_changes": [{"asset_id": "T1", "price": "0.30", "size": "5", "side": "BUY",
                           "best_bid": "0.30", "best_ask": "0.50"}],
    }))
    assert book.best_bid == 0.30 and book.best_ask == 0.50
