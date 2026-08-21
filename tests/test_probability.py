"""概率校准与 Edge 计算测试。"""

import pytest

from pm5hft.probability.calibration import Calibrator
from pm5hft.probability.edge import compute_edge, depth_weighted_ask
from pm5hft.probability.model import BaselineModel


def test_calibrator_cold_start_shrinks():
    cal = Calibrator(min_n=200, cold_start_shrink=0.85)
    r = cal.calibrate(0.99)
    assert r.cold is True
    assert r.cal_prob == pytest.approx(0.99 * 0.85)


def test_calibrator_converges_to_actual():
    cal = Calibrator(min_n=10, cold_start_shrink=0.85)
    for _ in range(10):
        cal.record(0.80, True)
    r = cal.calibrate(0.80)
    assert r.cold is False
    assert r.cal_prob == pytest.approx(1.0, abs=0.02)


def test_calibrator_brier_ece():
    cal = Calibrator(min_n=200)
    for _ in range(100):
        cal.record(0.60, True)   # 高估
        cal.record(0.80, False)  # 高估
    assert cal.brier() is not None
    assert cal.ece() is not None and cal.ece() > 0.3


def test_calibrator_99c_unsafe_flag():
    cal = Calibrator(min_n=5)
    for _ in range(5):
        cal.record(0.995, False)  # 99% 桶实际胜率 0
    r = cal.calibrate(0.995, market_price=0.985)
    assert r.tail_capture_unsafe is True


def test_edge_pipeline():
    e = compute_edge(cal_prob=0.55, ask=0.43, taker_fee=0.0001, remaining_s=120,
                     model_err=0.03, risk_buffer=0.005)
    assert e.gross_edge == pytest.approx(0.12)
    assert e.net_edge < e.gross_edge
    assert e.net_edge > 0.05  # 12% - 各项成本仍为正
    # 剩余时间越短，时间风险越大
    e2 = compute_edge(0.55, 0.43, 0.0001, 10, 0.03, 0.005)
    assert e2.net_edge < e.net_edge


def test_depth_weighted_ask():
    levels = [(0.43, 100.0), (0.44, 50.0)]
    avg, filled = depth_weighted_ask(levels, 120.0)
    assert avg == pytest.approx((0.43 * 100 + 0.44 * 20) / 120)
    assert filled == 120.0
    # 深度不足
    avg2, filled2 = depth_weighted_ask(levels, 300.0)
    assert filled2 == 150.0


def test_baseline_model_direction():
    m = BaselineModel()
    f = {"dist_bps": 30.0, "remaining_s": 120, "rv_60s": 0.001}
    p_up = m.predict(f)
    f2 = {"dist_bps": -30.0, "remaining_s": 120, "rv_60s": 0.001}
    p_down = m.predict(f2)
    assert p_up > 0.5 > p_down
    # 距离放大 → 更极端
    f3 = {"dist_bps": 300.0, "remaining_s": 120, "rv_60s": 0.001}
    assert m.predict(f3) > p_up
