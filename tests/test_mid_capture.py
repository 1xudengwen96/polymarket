"""中段买方实验模块（mid_capture）测试。

覆盖：94-96¢ 区间触发、时间窗口守卫、与主 tail 的同窗口互斥（双向）、
持有到结算（不出场）、独立记账、关闭开关回落。
"""

from decimal import Decimal

from pm5hft.config import Config
from pm5hft.probability.calibration import Calibrator
from pm5hft.probability.model import BaselineModel
from pm5hft.risk import RiskEngine
from pm5hft.strategy.engine import StrategyEngine


class FakeRec:
    market_id = 12
    asset = "btc"
    token_up = "T_UP_B"
    token_down = "T_DOWN_B"
    tick_size = "0.01"
    min_order_size = "5"


class FakeTwap:
    ptb = Decimal("63000")
    self_result = None


def features(**over) -> dict:
    base = dict(
        ts_ms=0, remaining_s=45.0, into_window_s=255.0,
        ptb=63000.0, twap_now=63005.0, dist_bps=5.0,
        up_bid=0.94, up_ask=0.95, down_bid=0.03, down_ask=0.04,
        up_spread=0.01, down_spread=0.01, obi3=0.0, obi10=0.0,
        up_microprice=0.955, pm_last_up=0.96, pm_chg_60s=0.0,
        tick_age_ms=50,
    )
    base.update(over)
    return base


def make_engine(cfg: Config) -> StrategyEngine:
    # 生产配置已关闭 mid_capture（实验终止），测试显式开启
    cfg.strategy["mid_capture"]["enabled"] = True
    # 生产配置：方向性禁用（99），tail 启用（纯价格闸门），mid/fade 启用
    risk = RiskEngine(cfg, mode="paper")
    return StrategyEngine(cfg, risk, BaselineModel(), Calibrator(min_n=1000))


def test_mid_fires_in_zone():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    f = features(remaining_s=45.0)  # up ask=0.95 ∈ [0.94,0.96) → 买 UP
    d = eng.decide(f, rec, wt)
    assert d.action == "MID_CAPTURE_UP", d
    assert d.side == "BUY" and d.post_only is True
    # maker = min(bid+1tick, ask-1tick) = min(0.95, 0.94) = 0.94
    assert Decimal(d.price) == Decimal("0.94"), d.price
    # 5 USDC @0.94 → 5.32 → 向下取整 5 股
    assert Decimal(d.qty) == Decimal("5"), d.qty
    # 挂单发出 → 防重发
    d2 = eng.decide(f, rec, wt)
    assert d2.action == "NOOP"


def test_mid_guards():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # 剩余 >60s → 未到入场窗口
    d = eng.decide(features(remaining_s=120.0), rec, wt)
    assert d.action == "NOOP"
    # 区间外：0.93 / 0.97
    d = eng.decide(features(up_ask=0.93, up_bid=0.92), rec, wt)
    assert d.action == "NOOP"
    d = eng.decide(features(up_ask=0.97, up_bid=0.96), rec, wt)
    assert d.action == "NOOP"
    # 剩余 <10s
    d = eng.decide(features(remaining_s=5.0), rec, wt)
    assert d.action == "NOOP"


def test_mutual_exclusion_both_directions():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # ① mid 先占（挂单中）→ tail 被拒
    d = eng.decide(features(remaining_s=50.0), rec, wt)
    assert d.action == "MID_CAPTURE_UP"
    f_tail = features(up_ask=0.99, up_bid=0.98, down_ask=0.01, down_bid=0.0, remaining_s=40.0)
    d2 = eng.decide(f_tail, rec, wt)
    assert not d2.action.startswith("TAIL_CAPTURE"), d2
    # 挂单过期 → 解锁 tail
    eng.on_order_expired(12)
    d3 = eng.decide(f_tail, rec, wt)
    assert d3.action.startswith("TAIL_CAPTURE"), d3
    # ② tail 先占（挂单中）→ mid 被拒
    rec2, wt2 = FakeRec(), FakeTwap()
    rec2.market_id = 13
    d4 = eng.decide(f_tail, rec2, wt2)
    assert d4.action.startswith("TAIL_CAPTURE")
    f_mid = features(up_ask=0.95, up_bid=0.94, remaining_s=35.0)
    d5 = eng.decide(f_mid, rec2, wt2)
    assert not d5.action.startswith("MID_CAPTURE"), d5


def test_mid_holds_to_settlement():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(remaining_s=50.0), rec, wt)
    assert d.action == "MID_CAPTURE_UP"
    assert d.exit_mode == "hold"  # market 12 偶数 → hold 变体
    eng.on_fill(12, "UP", Decimal(d.qty), Decimal(d.price), meta={"module": "mid_capture", "token_side": "UP"})
    pos = eng.positions[12]
    assert pos.mid_held is True and pos.up_qty > 0
    # 价格涨到 0.99（+4 tick）→ 仍持有，不 EXIT
    f2 = features(up_bid=0.99, up_ask=0.995, down_bid=0.0, down_ask=0.01, remaining_s=20.0)
    d2 = eng.decide(f2, rec, wt)
    assert not d2.action.startswith("EXIT"), d2
    # tail 也被互斥挡住
    assert not d2.action.startswith("TAIL_CAPTURE")


def test_mid_profit3_exits_on_sprint():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    rec.market_id = 11  # 奇数 → profit3 变体
    d = eng.decide(features(remaining_s=50.0), rec, wt)
    assert d.action == "MID_CAPTURE_UP"
    assert d.exit_mode == "profit3"
    eng.on_fill(11, "UP", Decimal(d.qty), Decimal(d.price),
                meta={"module": "mid_capture", "token_side": "UP", "exit_mode": "profit3"})
    pos = eng.positions[11]
    assert pos.mid_exit == "profit3"
    # 买价升到 0.97：tick=0.001，(0.97-0.94)/0.001=30 ticks → target 0.943 < bid → FAK 落袋
    f2 = features(up_bid=0.97, up_ask=0.98, down_bid=0.02, down_ask=0.03, remaining_s=30.0)
    d2 = eng.decide(f2, rec, wt)
    assert d2.action == "EXIT_PROFIT", d2
    assert d2.side == "SELL" and d2.tif == "FAK"
    assert Decimal(d2.price) == Decimal("0.97")
    # 出场成交 → 仓位清零、SELL 记入实验账本（不进主账本）
    eng.on_fill(11, "UP", Decimal(d2.qty), Decimal(d2.price), side="SELL", meta={"module": "exit"})
    assert eng.positions[11].up_qty == 0
    assert 11 in eng.mid_risk.windows
    assert 11 not in eng.risk.windows


def test_mid_profit3_holds_when_no_profit():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    rec.market_id = 11  # profit3
    d = eng.decide(features(remaining_s=50.0), rec, wt)
    eng.on_fill(11, "UP", Decimal(d.qty), Decimal(d.price),
                meta={"module": "mid_capture", "token_side": "UP", "exit_mode": "profit3"})
    # 价格不动（bid=0.94，0 ticks）→ 无 EXIT、无止损/时间止损
    f2 = features(remaining_s=5.0)
    d2 = eng.decide(f2, rec, wt)
    assert not d2.action.startswith("EXIT"), d2


def test_mid_isolated_accounting():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    main_eq0 = eng.risk.equity
    mid_eq0 = eng.mid_risk.equity
    d = eng.decide(features(remaining_s=50.0), rec, wt)
    qty, price = Decimal(d.qty), Decimal(d.price)
    eng.on_fill(12, "UP", qty, price, meta={"module": "mid_capture", "token_side": "UP"})
    assert eng.risk.equity == main_eq0
    assert 12 not in eng.risk.windows
    assert eng.mid_risk.windows[12].up_notional == qty * price
    eng.on_settlement(12, "UP", -(qty * price))
    assert eng.risk.equity == main_eq0
    assert eng.mid_risk.equity == mid_eq0 - (qty * price)


def test_mid_disabled_falls_back_to_tail():
    cfg = Config()
    cfg.strategy["mid_capture"]["enabled"] = False
    risk = RiskEngine(cfg, mode="paper")
    eng = StrategyEngine(cfg, risk, BaselineModel(), Calibrator(min_n=1000))
    rec, wt = FakeRec(), FakeTwap()
    # mid 关闭 → 94-96 区间无动作；99¢ 时 tail 正常触发
    d = eng.decide(features(remaining_s=50.0), rec, wt)
    assert not d.action.startswith("MID_CAPTURE"), d
    d2 = eng.decide(features(up_ask=0.99, up_bid=0.98, remaining_s=40.0), rec, wt)
    assert d2.action.startswith("TAIL_CAPTURE"), d2
