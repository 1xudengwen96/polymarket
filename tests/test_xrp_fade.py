"""XRP 反向实验模块（xrp_fade）测试。

覆盖：镜像触发、冷门方价格上限、窗口去重、持仓持有到结算、
成交/结算独立记账（不污染主风控）、关闭开关后回落到正常路径。
"""

from decimal import Decimal

from pm5hft.config import Config
from pm5hft.probability.calibration import Calibrator
from pm5hft.probability.model import BaselineModel
from pm5hft.risk import RiskEngine
from pm5hft.strategy.engine import StrategyEngine


class FakeRec:
    market_id = 77
    asset = "xrp"
    token_up = "T_UP_X"
    token_down = "T_DOWN_X"
    tick_size = "0.01"
    min_order_size = "5"


class FakeTwap:
    ptb = Decimal("2.10")
    self_result = None


def features(**over) -> dict:
    base = dict(
        ts_ms=0, remaining_s=180.0, into_window_s=20.0,
        ptb=2.10, twap_now=2.10, dist_bps=0.0,
        up_bid=0.99, up_ask=0.995, down_bid=0.01, down_ask=0.02,
        up_spread=0.005, down_spread=0.01, obi3=0.0, obi10=0.0,
        up_microprice=0.995, pm_last_up=0.995, pm_chg_60s=0.0,
        tick_age_ms=50,
    )
    base.update(over)
    return base


def make_engine(cfg: Config) -> StrategyEngine:
    # 生产配置已关闭 xrp_fade（实验终止），测试显式开启
    cfg.strategy["xrp_fade"]["enabled"] = True
    risk = RiskEngine(cfg, mode="paper")
    return StrategyEngine(cfg, risk, BaselineModel(), Calibrator(min_n=1000))


def test_fade_taker_buys_longshot():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    f = features(remaining_s=120.0)  # up 99.5¢ 热门 → 买 DOWN
    d = eng.decide(f, rec, wt)
    assert d.action == "XRP_FADE_DOWN", d
    assert d.side == "BUY" and d.token_side == "DOWN"
    # taker 模式：FAK 直接吃 ask=0.02，固定 5 股
    assert d.tif == "FAK" and d.post_only is False
    assert Decimal(d.price) == Decimal("0.02"), d.price
    assert Decimal(d.qty) == Decimal("5"), d.qty
    # 挂单发出 → 防重发
    d2 = eng.decide(f, rec, wt)
    assert d2.action == "NOOP"


def test_fade_maker_mode_regression():
    cfg = Config()
    cfg.strategy["xrp_fade"]["mode"] = "maker"
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(remaining_s=120.0), rec, wt)
    assert d.action == "XRP_FADE_DOWN"
    assert d.tif == "GTD" and d.post_only is True
    assert Decimal(d.price) == Decimal("0.011"), d.price  # down_bid+1tick
    assert Decimal(d.qty) == Decimal("450"), d.qty  # 5 USDC @0.011 向下取整


def test_fade_skips_when_longshot_too_expensive():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # 热门 up 98¢ 但冷门 DOWN ask=0.04 > max_longshot_price=0.03 → 不买
    f = features(up_ask=0.98, up_bid=0.979, down_ask=0.04, down_bid=0.03, remaining_s=120.0)
    d = eng.decide(f, rec, wt)
    assert d.action == "NOOP", d


def test_fade_both_sides_symmetric():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # DOWN 热门 → 买 UP（taker 吃 up_ask=0.015）
    f = features(down_ask=0.99, down_bid=0.985, up_ask=0.015, up_bid=0.005, remaining_s=120.0)
    d = eng.decide(f, rec, wt)
    assert d.action == "XRP_FADE_UP", d
    assert d.tif == "FAK" and Decimal(d.price) == Decimal("0.015"), d


def test_fade_holds_to_settlement_no_exit():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(remaining_s=120.0), rec, wt)
    assert d.action == "XRP_FADE_DOWN"
    eng.on_fill(77, "DOWN", Decimal(d.qty), Decimal(d.price), meta={"module": "xrp_fade", "token_side": "DOWN"})
    pos = eng.positions[77]
    assert pos.fade_held is True and pos.down_qty > 0
    # 冷门方暴涨到 0.90（翻盘进行中）→ 仍持有，不触发任何 EXIT
    f2 = features(remaining_s=30.0, down_bid=0.89, down_ask=0.90, up_bid=0.09, up_ask=0.10)
    d2 = eng.decide(f2, rec, wt)
    assert not d2.action.startswith("EXIT"), d2
    assert d2.action == "NOOP"


def test_fade_isolated_accounting():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    main_eq0 = eng.risk.equity
    fade_eq0 = eng.fade_risk.equity
    d = eng.decide(features(remaining_s=120.0), rec, wt)
    qty, price = Decimal(d.qty), Decimal(d.price)
    # 成交只进实验风控
    eng.on_fill(77, "DOWN", qty, price, meta={"module": "xrp_fade", "token_side": "DOWN"})
    assert eng.risk.equity == main_eq0
    assert eng.fade_risk.equity == fade_eq0  # 成交不动权益，结算才动
    assert 77 not in eng.risk.windows  # 主风控无该窗口敞口记录
    assert eng.fade_risk.windows[77].down_notional == qty * price
    # 结算（DOWN 输，归零）→ 实验亏 5 USDC，主账本不动
    eng.on_settlement(77, "UP", -(qty * price))
    assert eng.risk.equity == main_eq0
    assert eng.fade_risk.equity == fade_eq0 - (qty * price)


def test_fade_disabled_falls_back_to_normal_path():
    cfg = Config()
    cfg.strategy["xrp_fade"]["enabled"] = False
    risk = RiskEngine(cfg, mode="paper")
    eng = StrategyEngine(cfg, risk, BaselineModel(), Calibrator(min_n=1000))
    rec, wt = FakeRec(), FakeTwap()
    # fade 关闭 → xrp 回到主路径：方向性已禁用（entry_min_gross_edge=99）
    # 而 tail 在主路径上会对 99¢ 热门侧下单（纯价格闸门）
    d = eng.decide(features(up_ask=0.99, up_bid=0.98, down_ask=0.02, down_bid=0.01, remaining_s=120.0), rec, wt)
    assert not d.action.startswith("XRP_FADE"), d


def test_periodic_daily_reset_both_ledgers():
    cfg = Config()
    eng = make_engine(cfg)
    eng.risk.daily_pnl = Decimal("-10")
    eng.fade_risk.daily_pnl = Decimal("-130")
    eng.fade_risk.hourly_pnl = Decimal("-40")
    day0 = 1786867000000  # UTC 某日
    eng._maybe_reset_periodic(day0)
    assert eng.fade_risk.daily_pnl == Decimal("-130")  # 同日不重置
    next_day = day0 + 86_400_000
    eng._maybe_reset_periodic(next_day)
    assert eng.risk.daily_pnl == Decimal("0")
    assert eng.fade_risk.daily_pnl == Decimal("0")
    assert eng.fade_risk.hourly_pnl == Decimal("-40")  # 小时边界未跨 → 不重置
