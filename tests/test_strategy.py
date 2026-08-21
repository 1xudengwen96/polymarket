"""策略状态机测试。"""

from decimal import Decimal

from pm5hft.config import Config
from pm5hft.probability.calibration import Calibrator
from pm5hft.probability.model import BaselineModel
from pm5hft.risk import RiskEngine
from pm5hft.strategy import S3_WAIT_REPRICING, S5_COMPLETE_SET, StrategyEngine


class FakeRec:
    market_id = 42
    asset = "btc"
    token_up = "T_UP"
    token_down = "T_DOWN"
    tick_size = "0.01"
    min_order_size = "5"


class FakeTwap:
    ptb = Decimal("63200")
    self_result = None


def make_engine(cfg: Config) -> StrategyEngine:
    # 方向性入场测试需要低于禁用阈值的 gross 门槛（生产配置=99 已禁用方向性入场）
    cfg.strategy["entry"]["entry_min_gross_edge"] = 0.06
    risk = RiskEngine(cfg, mode="paper")
    return StrategyEngine(cfg, risk, BaselineModel(), Calibrator(min_n=1000))


def features(**over) -> dict:
    base = dict(
        ts_ms=0, remaining_s=180.0, into_window_s=20.0,
        ptb=63200.0, twap_now=63205.0, dist_bps=8.0,
        approx_twap60=63205.0, approx_twap30=63205.0, btc_price=63205.0,
        ret_1s=0.0, ret_3s=0.0, ret_5s=0.0, ret_10s=0.0, ret_30s=0.0, ret_60s=0.0,
        rv_5s=0.0002, rv_30s=0.0005, rv_60s=0.0008,
        vol_1s=1.0, vol_10s=10.0, vol_60s=60.0,
        agg_buy_5s=1.0, agg_sell_5s=1.0, agg_buy_30s=5.0, agg_sell_30s=5.0,
        tfi_5s=0.0, tfi_30s=0.0, cvd=0.0, accel_5s=0.0, reversal_score=0.5,
        tick_age_ms=50,
        up_bid=0.52, up_ask=0.53, down_bid=0.46, down_ask=0.47,
        up_spread=0.01, down_spread=0.01, obi3=0.0, obi10=0.0, up_microprice=0.525,
        pm_last_up=0.53, pm_chg_60s=0.0,
    )
    base.update(over)
    return base


def test_entry_then_exit_profit(cfg=None):
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # Up ask=0.43（深度便宜）→ 应进入
    f = features(up_ask=0.43, up_bid=0.42, dist_bps=80.0, remaining_s=200.0)
    d = eng.decide(f, rec, wt)
    assert d.action == "ENTER_UP", d
    assert d.post_only is True
    # 模拟成交
    eng.on_fill(42, "UP", Decimal(d.qty), Decimal(d.price))
    eng.on_entry(42, "UP")
    assert eng.positions[42].state == S3_WAIT_REPRICING
    # 重新定价到位 → 获利退出
    f2 = features(up_ask=0.52, up_bid=0.51, down_ask=0.48, down_bid=0.47, remaining_s=150.0)
    d2 = eng.decide(f2, rec, wt)
    assert d2.action == "EXIT_PROFIT", d2


def test_entry_then_hedge():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    f = features(up_ask=0.43, up_bid=0.42, dist_bps=80.0, remaining_s=200.0)
    d = eng.decide(f, rec, wt)
    assert d.action == "ENTER_UP"
    eng.on_fill(42, "UP", Decimal(d.qty), Decimal("0.42"))
    eng.on_entry(42, "UP")
    # Down 便宜到 0.47 → 对冲（0.42+0.47=0.89 < 1）；
    # Up 只回到 0.43（1 tick，未达 3-tick 获利退出线）→ 不触发 EXIT_PROFIT
    f2 = features(up_ask=0.44, up_bid=0.43, down_ask=0.47, down_bid=0.46, remaining_s=140.0)
    d2 = eng.decide(f2, rec, wt)
    assert d2.action == "HEDGE_DOWN", d2
    eng.on_fill(42, "DOWN", Decimal(d2.qty), Decimal(d2.price))
    assert eng.positions[42].state == S5_COMPLETE_SET


def test_no_martingale_after_entry():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    f = features(up_ask=0.43, up_bid=0.42, dist_bps=80.0)
    d = eng.decide(f, rec, wt)
    assert d.action == "ENTER_UP"
    eng.on_fill(42, "UP", Decimal(d.qty), Decimal(d.price))
    eng.on_entry(42, "UP")
    # 价格继续走低（Up 0.40）→ 状态机在持仓分支，不会再加仓 Up（铁律）
    f2 = features(up_ask=0.40, up_bid=0.39, down_ask=0.60, down_bid=0.59, remaining_s=150.0)
    d2 = eng.decide(f2, rec, wt)
    assert d2.action not in ("ENTER_UP",), "martingale violation!"
    # 且不强行对冲（0.42+0.60>1）
    assert d2.action != "HEDGE_DOWN"


def test_tail_capture_requires_edge():
    cfg = Config()
    cfg.strategy["tail_capture"]["use_model_gate"] = True  # 该测试验证模型闸门行为
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # 99¢ 但模型无法支撑 → NOOP
    f = features(up_ask=0.99, up_bid=0.98, dist_bps=0.0, remaining_s=60.0)
    d = eng.decide(f, rec, wt)
    assert d.action == "NOOP" or not d.action.startswith("TAIL_CAPTURE")


def test_arb_detection():
    cfg = Config()
    cfg.strategy["arb"]["enabled"] = True  # 生产配置已禁用 arb，该测试显式开启
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # remaining=20s 使 entry 分支被时间守卫拦截（<30s），落到 ARB 模块
    f = features(up_ask=0.44, up_bid=0.43, down_ask=0.44, down_bid=0.43, remaining_s=20.0)
    d = eng.decide(f, rec, wt)
    # 0.44+0.44=0.88 → 套利空间 12% 远超阈值
    assert d.action == "ARB", d


def test_tail_min_notional_floor_small_equity():
    """固定 5 USDC 必须向上计算数量，最终订单金额不得低于 5。"""
    cfg = Config()
    risk = RiskEngine(cfg, mode="live")  # live 风控档（未对冲 12% 等）
    risk.set_equity(Decimal("92"))
    eng = StrategyEngine(cfg, risk, BaselineModel(), Calibrator(min_n=1000))
    rec, wt = FakeRec(), FakeTwap()
    f = features(up_ask=0.99, up_bid=0.98, down_ask=0.02, down_bid=0.01, remaining_s=60.0)
    d = eng.decide(f, rec, wt)
    assert d.action.startswith("TAIL_CAPTURE"), d
    assert Decimal(d.qty) >= Decimal("5")
    assert Decimal(d.qty) * Decimal(d.price) >= Decimal("5")


def test_auto_trading_disabled_blocks_new_tail_entry():
    cfg = Config()
    eng = make_engine(cfg)
    eng.set_runtime_controls(False, Decimal("5"))
    rec, wt = FakeRec(), FakeTwap()
    f = features(up_ask=0.99, up_bid=0.98, down_ask=0.02, down_bid=0.01, remaining_s=60.0)
    d = eng.decide(f, rec, wt)
    assert d.action == "NOOP"
    assert d.reject_code == "AUTO_TRADING_DISABLED"


def test_tail_blocked_when_book_stale():
    """簿口滞后（>book_stale_ms）→ 禁止入场（BOOK_STALE 熔断）。"""
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    f = features(up_ask=0.99, up_bid=0.98, down_ask=0.02, down_bid=0.01,
                 remaining_s=60.0, up_book_age_ms=5000)
    d = eng.decide(f, rec, wt)
    assert not d.action.startswith("TAIL_CAPTURE"), d
    assert d.reject_code == "BOOK_STALE" or "book stale" in (d.reason or "")
