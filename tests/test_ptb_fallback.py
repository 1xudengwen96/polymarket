"""PTB 兜底测试：spot 缓冲重建 + TwapService 断流兜底/超时。"""

from decimal import Decimal

from pm5hft.clock import AssetWindow
from pm5hft.config import Config
from pm5hft.features import TickBuffer
from pm5hft.twap import TwapService, rebuild_ptb_from_buffer


class FakeRepo:
    def __init__(self) -> None:
        self.saved: list = []

    async def insert_twap_sample(self, *a, **k):  # noqa: ANN002, ANN003
        pass

    async def get_market(self, *a, **k):  # noqa: ANN002, ANN003
        return None

    async def save_settlement(self, *a, **k):  # noqa: ANN002, ANN003
        self.saved.append((a, k))

    async def get_settlement(self, *a, **k):  # noqa: ANN002, ANN003
        return None

    async def mark_settlement_reconciled(self, *a, **k):  # noqa: ANN002, ANN003
        self.saved.append(("reconciled", a, k))


def test_rebuild_ptb_basic():
    buf = TickBuffer()
    t_start = 1_000_000
    for i in range(60):
        buf.feed_bar(t_start * 1000 - 60_000 + i * 1000, 0, 0, 0, 100.0 + i, 0, 0, 0, 1)
    v = rebuild_ptb_from_buffer(buf, t_start, 60)
    assert v == Decimal("129.5")  # mean(100..159)


def test_rebuild_ptb_insufficient_coverage():
    buf = TickBuffer()
    t_start = 1_000_000
    for i in range(20):
        buf.feed_bar(t_start * 1000 - 60_000 + i * 1000, 0, 0, 0, 100.0, 0, 0, 0, 1)
    # 序列末端距窗口起点 41s → 拒绝（末段全缺）
    assert rebuild_ptb_from_buffer(buf, t_start, 60) is None


def test_rebuild_ptb_sparse_with_forward_fill():
    buf = TickBuffer()
    t_start = 1_000_000
    t0 = t_start * 1000 - 60_000
    for i in range(30):  # 隔秒采样（30/60 覆盖），价格 100+i
        buf.feed_bar(t0 + 2 * i * 1000, 0, 0, 0, 100.0 + i, 0, 0, 0, 1)
    v = rebuild_ptb_from_buffer(buf, t_start, 60)
    # 前值填充：每秒取 ≤该秒的最近 bar 值 → 均值 = 2*Σ(100..129)/60 = 114.5
    assert v == Decimal("114.5")


def test_rebuild_ptb_stale_buffer_rejected():
    buf = TickBuffer()
    t_start = 1_000_000
    for i in range(60):
        buf.feed_bar(t_start * 1000 - 70_000 + i * 1000, 0, 0, 0, 100.0, 0, 0, 0, 1)
    # 缓冲在窗口起点前 11s 就断了 → 拒绝（截断样本不能冒充 PTB）
    assert rebuild_ptb_from_buffer(buf, t_start, 60) is None


async def test_twap_service_fallback_on_stall():
    cfg = Config()
    repo = FakeRepo()
    calls = {}

    def fb(asset, ts, lookback):  # noqa: ANN001
        calls["k"] = (asset, ts, lookback)
        return Decimal("123.45")

    svc = TwapService(cfg, repo, binance_twap=fb)
    win = AssetWindow(asset="btc", tf_label="5m", t_start=1_000_000, duration_s=300)
    svc.on_window_start(cfg.asset("btc"), win, 60)
    # 窗口开始 7s 后仍无 RTDS PTB → 兜底触发（先于 15s 超时）
    await svc.check_timeouts(now=1_000_000 + 7)
    wt = svc.current("btc")
    assert wt is not None
    assert wt.ptb == Decimal("123.45")
    assert wt.ptb_src == "spot_rebuilt"
    assert calls["k"] == ("btc", 1_000_000, 60)


async def test_twap_service_timeout_when_fallback_fails():
    cfg = Config()
    repo = FakeRepo()
    svc = TwapService(cfg, repo, binance_twap=lambda a, t, lb: None)
    win = AssetWindow(asset="btc", tf_label="5m", t_start=1_000_000, duration_s=300)
    svc.on_window_start(cfg.asset("btc"), win, 60)
    await svc.check_timeouts(now=1_000_000 + 7)  # 兜底失败但未到超时线
    wt = svc.current("btc")
    assert wt is not None and wt.ptb is None and wt.self_result is None
    await svc.check_timeouts(now=1_000_000 + 16)  # 15s 超时 → 窗口作废
    assert wt.ptb_src == "missing"
    assert wt.self_result == "UNKNOWN"


async def test_final_rebuilt_from_spot():
    cfg = Config()
    repo = FakeRepo()
    calls = {}

    def fb(asset, ts, lookback):  # noqa: ANN001
        calls["k"] = (asset, ts, lookback)
        return Decimal("100.0") if ts == 1_000_000 else Decimal("101.5")

    svc = TwapService(cfg, repo, binance_twap=fb)
    win = AssetWindow(asset="btc", tf_label="5m", t_start=1_000_000, duration_s=300)
    svc.on_window_start(cfg.asset("btc"), win, 60)
    wt = svc.current("btc")
    assert wt is not None
    wt.ptb = Decimal("100.0")
    wt.ptb_src = "rtds"
    # t_end+9s：final 缺失 → spot 重建 final=101.5；但 PTB 官方/final 非官方 → 混合来源
    # 裁决搁置为 UNKNOWN，等待 gamma（结算只认官方对）
    await svc.check_timeouts(now=1_000_300 + 9)
    assert wt.final == Decimal("101.5")
    assert wt.final_src == "spot_rebuilt"
    assert wt.self_result == "UNKNOWN"
    assert calls["k"] == ("btc", 1_000_300, 60)


class FakeRec:
    market_id = 77
    t_start = 1_000_000
    gamma_closed = True
    gamma_outcome_prices = ["1", "0"]


async def test_gamma_settles_withheld_window():
    cfg = Config()
    repo = FakeRepo()
    settled = {}

    async def on_gamma_settle(mid, result):  # noqa: ANN001
        settled["k"] = (mid, result)

    svc = TwapService(cfg, repo, binance_twap=lambda a, t, lb: Decimal("101.5"),
                      on_gamma_settle=on_gamma_settle)
    win = AssetWindow(asset="btc", tf_label="5m", t_start=1_000_000, duration_s=300)
    svc.on_window_start(cfg.asset("btc"), win, 60)
    wt = svc.current("btc")
    assert wt is not None
    wt.ptb = Decimal("100.0")
    wt.ptb_src = "spot_rebuilt"  # 非官方 PTB
    # final 从 spot 兜底重建 → 混合 → 搁置 UNKNOWN
    await svc.check_timeouts(now=1_000_300 + 9)
    assert wt.self_result == "UNKNOWN"
    # gamma 官方 UP → 回调结账
    await svc.reconcile_gamma("btc", FakeRec())
    assert settled["k"] == (77, "UP")


async def test_self_settle_official_pair():
    cfg = Config()
    repo = FakeRepo()
    svc = TwapService(cfg, repo)
    win = AssetWindow(asset="btc", tf_label="5m", t_start=1_000_000, duration_s=300)
    svc.on_window_start(cfg.asset("btc"), win, 60)
    wt = svc.current("btc")
    assert wt is not None
    wt.ptb = Decimal("100.0")
    wt.ptb_src = "rtds"
    # RTDS final 样本到达 → 官方对 → 正常自结算
    await svc.on_twap("btc/usd", 60, Decimal("101.5"), "101.5", 1_000_300 * 1000)
    assert wt.final_src == "rtds"
    assert wt.self_result == "UP"


async def test_final_timeout_when_rebuild_fails():
    cfg = Config()
    repo = FakeRepo()
    svc = TwapService(cfg, repo, binance_twap=lambda a, t, lb: None)
    win = AssetWindow(asset="btc", tf_label="5m", t_start=1_000_000, duration_s=300)
    svc.on_window_start(cfg.asset("btc"), win, 60)
    wt = svc.current("btc")
    assert wt is not None
    wt.ptb = Decimal("100.0")
    await svc.check_timeouts(now=1_000_300 + 9)  # 重建失败但未超时
    assert wt.final is None and wt.self_result is None
    await svc.check_timeouts(now=1_000_300 + 21)  # 20s 超时 → UNKNOWN
    assert wt.self_result == "UNKNOWN"
