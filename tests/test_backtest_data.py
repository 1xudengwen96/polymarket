"""回测数据层单元测试：TWAP 标签重建、覆盖度、防 lookahead 语义。"""

from decimal import Decimal

from pm5hft.backtest.data import twap_from_bars, twap_from_trades


def _bars(start_ms: int, n: int, step: float = 1.0, gap_every: int | None = None) -> list[tuple[int, float]]:
    out = []
    for i in range(n):
        if gap_every and i % gap_every == 0 and i > 0:
            continue  # 模拟缺失秒
        out.append((start_ms + i * 1000, 100.0 + i * step))
    return out


def test_twap_mean():
    bars = _bars(1_000_000_000, 60, step=1.0)
    v = twap_from_bars(bars, 1_000_000_000 + 60_000, 60)
    # 均值 = 100..159 → 129.5
    assert v == Decimal("129.5")


def test_twap_coverage_insufficient():
    bars = _bars(1_000_000_000, 20)  # 只有 20s 数据
    v = twap_from_bars(bars, 1_000_000_000 + 60_000, 60)
    assert v is None


def test_twap_stale_data_rejected():
    bars = _bars(1_000_000_000, 60)
    # 边界后 10s 无新数据 → 陈旧
    v = twap_from_bars(bars, 1_000_000_000 + 70_000, 60)
    assert v is None


def test_twap_gap_tolerance():
    bars = _bars(1_000_000_000, 60, gap_every=5)  # 缺失 ~12 秒（80% 覆盖线内）
    v = twap_from_bars(bars, 1_000_000_000 + 60_000, 60)
    assert v is not None


def test_tie_goes_up():
    t0 = 1_000_000_000
    bars = [(t, 100.0) for t in range(t0 - 120_000, t0 + 60_000, 1000)]
    ptb = twap_from_bars(bars, t0, 60)
    final = twap_from_bars(bars, t0 + 60_000, 60)
    assert ptb is not None and final is not None
    assert final == ptb == Decimal("100.0")
    assert final >= ptb  # 平局算 UP（市场规则）


# ── 逐笔时间加权 TWAP ───────────────────────────────────────
def _trades_us(n: int, step_us: int = 500_000, price0: float = 100.0) -> list[tuple[int, float]]:
    return [(1_000_000_000_000 + i * step_us, price0 + i * 0.01) for i in range(n)]


def test_twap_trades_uniform_equals_mean():
    # 每 0.5s 一笔，价格线性：时间加权 = 简单均值
    trades = _trades_us(120, step_us=500_000)
    v = twap_from_trades(trades, 1_000_000_000_000 + 60_000_000, 60)
    assert v is not None
    # 窗口内 120 笔（每笔 0.5s），均值 = 100 + 0.01×59.5
    assert abs(float(v) - 100.595) < 0.05


def test_twap_trades_gap_coverage():
    # 只有 10s 数据 → 覆盖不足
    trades = _trades_us(20, step_us=500_000)
    v = twap_from_trades(trades, 1_000_000_000_000 + 60_000_000, 60)
    assert v is None


def test_twap_trades_no_lookahead():
    # 边界之后的成交不得影响边界 TWAP
    trades = _trades_us(120, step_us=500_000)
    v1 = twap_from_trades(trades, 1_000_000_000_000 + 60_000_000, 60)
    trades2 = trades + [(1_000_000_000_000 + 60_500_000, 9999.0)]  # 边界后巨量变化
    v2 = twap_from_trades(trades2, 1_000_000_000_000 + 60_000_000, 60)
    assert v1 == v2
