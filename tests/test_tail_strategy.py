"""尾部策略单元测试：触发条件、去重、maker 挂单、免止损。"""

from decimal import Decimal

from pm5hft.config import Config
from pm5hft.probability.calibration import Calibrator
from pm5hft.risk import RiskEngine
from pm5hft.strategy import StrategyEngine


class FakeModel:
    """固定输出 0.995（高置信 Up），用于触发尾部逻辑。"""

    def predict(self, f: dict) -> float:
        return 0.995


class FakeRec:
    market_id = 7
    asset = "btc"
    token_up = "T_UP"
    token_down = "T_DOWN"
    tick_size = "0.01"
    min_order_size = "5"


class FakeTwap:
    ptb = Decimal("63200")
    self_result = None


def make_engine(cfg: Config) -> StrategyEngine:
    risk = RiskEngine(cfg, mode="paper")
    # 预置 99¢ 桶校准：实际胜率 0.996（249 胜 / 1 负，模型确认尾部安全）
    cal = Calibrator(min_n=200)
    for _ in range(249):
        cal.record(0.995, True)
    cal.record(0.995, False)
    return StrategyEngine(cfg, risk, FakeModel(), cal)


def features(**over) -> dict:
    base = dict(
        ts_ms=0, remaining_s=120.0, into_window_s=30.0,
        ptb=63200.0, twap_now=63205.0, dist_bps=8.0,
        ret_1s=0.0, ret_3s=0.0, ret_5s=0.0, ret_10s=0.0, ret_30s=0.0, ret_60s=0.0,
        rv_5s=0.0002, rv_30s=0.0005, rv_60s=0.0008,
        vol_1s=1.0, vol_10s=10.0, vol_60s=60.0,
        agg_buy_5s=1.0, agg_sell_5s=1.0, agg_buy_30s=5.0, agg_sell_30s=5.0,
        tfi_5s=0.0, tfi_30s=0.0, cvd=0.0, accel_5s=0.0, reversal_score=0.5,
        tick_age_ms=50,
        up_bid=0.989, up_ask=0.990, down_bid=0.009, down_ask=0.010,
        up_spread=0.001, down_spread=0.001, obi3=0.0, obi10=0.0, up_microprice=0.9895,
        pm_last_up=0.99, pm_chg_60s=0.0,
    )
    base.update(over)
    return base


def test_tail_capture_fires_when_edge_present():
    cfg = Config()
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # 禁用方向性入场：把 gross 门槛抬到 99
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    # up 在尾部 0.99，校准 0.996 ≥ 0.99+0.003 → 触发
    d = eng.decide(features(), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP", d
    assert d.side == "BUY"
    assert d.post_only is True
    assert d.tif == "GTD"
    # 挂单价 = min(bid+1tick, ask-1tick, 限价 0.98) = min(0.99, 0.989, 0.98) = 0.98
    assert Decimal(d.price) == Decimal("0.98")
    assert Decimal(d.qty) * Decimal(d.price) >= Decimal("5")


def test_tail_no_restack_same_window():
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP"
    # 挂单未成交前：不得重发（tail_pending）
    d2 = eng.decide(features(), rec, wt)
    assert d2.action == "NOOP", d2


def test_tail_no_cross_window_stacking():
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP"
    eng.on_fill(7, "UP", Decimal(d.qty), Decimal(d.price), "BUY", {"module": "tail_capture"})
    # 另一窗口（market_id=8，同资产 btc）同刻出现尾部机会 → 同资产锁拒绝
    rec2 = FakeRec()
    rec2.market_id = 8
    d2 = eng.decide(features(), rec2, wt)
    assert d2.action == "NOOP", d2


def test_tail_cross_asset_parallel_allowed():
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    # This test isolates the per-asset position lock. Production is currently
    # BTC-only, so explicitly provide enough test risk budget for two assets.
    cfg.risk.unhedged_exposure_per_asset_pct = 0.02
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP"
    eng.on_fill(7, "UP", Decimal(d.qty), Decimal(d.price), "BUY", {"module": "tail_capture"})
    # 另一资产（eth）同刻出现尾部机会 → 按资产锁：允许并行持仓
    rec2 = FakeRec()
    rec2.market_id = 8
    rec2.asset = "eth"
    d2 = eng.decide(features(), rec2, wt)
    assert d2.action == "TAIL_CAPTURE_UP", d2


def test_tail_hold_to_settlement_no_stop():
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(), rec, wt)
    eng.on_fill(7, "UP", Decimal(d.qty), Decimal(d.price), "BUY", {"module": "tail_capture"})
    pos = eng.positions[7]
    assert pos.tail_held is True
    # 价格大跌：尾部持仓不得触发止损
    d2 = eng.decide(features(up_bid=0.70, up_ask=0.71, down_bid=0.29, down_ask=0.30,
                             remaining_s=60.0), rec, wt)
    assert d2.action != "EXIT_STOP", d2


def test_tail_no_trigger_below_98():
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(up_bid=0.949, up_ask=0.950, down_bid=0.049, down_ask=0.050), rec, wt)
    assert d.action == "NOOP", d


def _enter_tail(cfg, price="0.980"):
    """构造一个 avg=price 的尾部持仓。"""
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP"
    eng.on_fill(7, "UP", Decimal(d.qty), Decimal(d.price), "BUY", {"module": "tail_capture"})
    # 覆盖 avg 为指定价格（模拟以 price 成交）
    eng.positions[7].avg_up = Decimal(price)
    return eng, rec, wt


def test_tail_never_exits_even_at_max_profit():
    """出场路径显式封死：tail 持仓无论利润多大、无论 maker/taker 模式都不出场。

    数据结论（纸面 369 笔反事实）：持有到结算 -4.95 优于所有出场变体
    （+3tick 锁定 -12.85；末段出场 -11.95~-15.60），天鹅不从高价崩盘，
    利润锁定救不了天鹅，出场只会割肉给赢单。
    """
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng, rec, wt = _enter_tail(cfg, price="0.980")
    # taker 模式：bid 冲到 0.994（14 tick 利润）也不得出场
    d = eng.decide(features(up_bid=0.994, up_ask=0.995, down_bid=0.005, down_ask=0.006,
                            remaining_s=60.0), rec, wt)
    assert d.action == "NOOP", d
    assert not d.action.startswith("EXIT"), d
    assert eng.positions[7].exit_pending is False  # 未产生出场挂单
    # maker 模式同样封死
    cfg.strategy["exit"]["exit_mode"] = "maker"
    d2 = eng.decide(features(up_bid=0.994, up_ask=0.995, down_bid=0.005, down_ask=0.006,
                             remaining_s=60.0), rec, wt)
    assert d2.action == "NOOP", d2
    assert not d2.action.startswith("EXIT"), d2
    assert eng.positions[7].exit_pending is False


def test_non_tail_position_exit_path_intact():
    """出场机制本身保留（方向性/中段仓位用）：非 tail 持仓在 +3tick 仍触发 EXIT_PROFIT。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    cfg.strategy["exit"]["exit_mode"] = "maker"
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.on_fill(7, "UP", Decimal("5"), Decimal("0.977"), "BUY",
                {"module": "entry", "first_fill": True})
    pos = eng.positions[7]
    assert pos.tail_held is False and pos.up_qty > 0
    # bid=0.980（尾区 tick=0.001）→ profit=3tick，target=avg+0.003=0.980 ≥ bid → 挂 post-only maker 出场
    d = eng.decide(features(up_bid=0.980, up_ask=0.981, down_bid=0.019, down_ask=0.020,
                            remaining_s=60.0), rec, wt)
    assert d.action == "EXIT_PROFIT", d
    assert d.post_only is True and d.tif == "GTD"
    assert Decimal(d.price) == Decimal("0.980")  # avg 0.977 + 3×0.001


def test_maker_exit_fill_clears_pending():
    """非 tail 持仓的 maker 出场成交 → exit_pending 复位、仓位清零。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    cfg.strategy["exit"]["exit_mode"] = "maker"
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.on_fill(7, "UP", Decimal("5"), Decimal("0.977"), "BUY",
                {"module": "entry", "first_fill": True})
    d = eng.decide(features(up_bid=0.980, up_ask=0.981, down_bid=0.019, down_ask=0.020,
                            remaining_s=60.0), rec, wt)
    assert d.action == "EXIT_PROFIT"
    assert eng.positions[7].exit_pending is True
    eng.on_fill(7, "UP", Decimal(d.qty), Decimal(d.price), "SELL", {"module": "exit"})
    assert eng.positions[7].exit_pending is False
    assert eng.positions[7].up_qty == 0


def test_directional_entry_disabled_even_with_one_sided_book():
    """回归：单边盘口（market_p=None）时 gross 阈值不得被绕过，方向性入场必须禁用。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # up 只有 ask（1¢），down 只有 bid（99¢）→ market_p=None；此前会放行 ENTER_UP
    d = eng.decide(features(up_bid=None, up_ask=0.01, down_bid=0.99, down_ask=None,
                            remaining_s=120.0), rec, wt)
    assert d.action not in ("ENTER_UP", "ENTER_DOWN"), d


def test_tail_price_only_gate_when_model_gate_off():
    """use_model_gate=false：低置信模型（cal 远低于价格）仍按纯价格闸门触发。"""
    from pm5hft.probability.calibration import Calibrator
    from pm5hft.risk import RiskEngine
    from pm5hft.strategy import StrategyEngine

    class LowModel:
        def predict(self, f: dict) -> float:
            return 0.5

    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    cfg.strategy["tail_capture"]["use_model_gate"] = False
    risk = RiskEngine(cfg, mode="paper")
    cal = Calibrator(min_n=200)
    for _ in range(249):
        cal.record(0.995, True)
    cal.record(0.995, False)
    eng = StrategyEngine(cfg, risk, LowModel(), cal)
    rec, wt = FakeRec(), FakeTwap()
    # up_ask=0.99 在触发区；模型 cal≈0.43 远低于门槛 → 闸门关闭时应触发
    d = eng.decide(features(), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP", d


def test_tail_entry_price_runtime_override():
    """前端可配进场价（限价语义）：默认 0.98 时 ask=0.85 不参与；调到 0.80 后参与且挂单价不高于 0.80。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # 默认进场价 0.98：ask=0.85 低于参与门槛 → 不触发
    d = eng.decide(features(up_ask=0.85, up_bid=0.84), rec, wt)
    assert d.action == "NOOP", d
    # 前端把进场价调到 0.80 → ask=0.85 参与，但挂单价被限价截到 0.80（市场不跌到 0.80 不成交）
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.80"))
    d = eng.decide(features(up_ask=0.85, up_bid=0.84), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP", d
    assert d.side == "BUY" and d.post_only is True
    # 挂单价 = min(bid+1tick=0.85, ask-1tick=0.84, 限价0.80) = 0.80
    assert Decimal(d.price) == Decimal("0.80")
    assert Decimal(d.qty) * Decimal(d.price) >= Decimal("5")


def test_tail_entry_price_cap_on_fill():
    """限价模式：市场价再高，成交价也不超过用户输入（如设 0.90，ask=0.94 → 挂 0.90）。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.90"))
    d = eng.decide(features(up_ask=0.94, up_bid=0.93), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP", d
    assert Decimal(d.price) == Decimal("0.90")


def test_tail_entry_price_clamped():
    """越界进场价被夹到 [0.50, 0.999]。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.30"))   # 过低 → 0.50
    assert eng.tail_entry_price == Decimal("0.50")
    eng.set_runtime_controls(True, Decimal("5"), Decimal("1.50"))   # 过高 → 0.999
    assert eng.tail_entry_price == Decimal("0.999")


def test_tail_low_price_ev_guard_blocks():
    """安全阀：进场价 < 0.95 时强制模型闸门，校准概率不足（74% vs ask 85¢）→ 拒绝。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    cfg.strategy["tail_capture"]["use_model_gate"] = False

    class MidModel:
        def predict(self, f: dict) -> float:
            return 0.80  # 校准桶无样本 → 冷启动 cal≈0.68 < 0.85+buffer

    risk = RiskEngine(cfg, mode="paper")
    cal = Calibrator(min_n=200)
    for _ in range(249):
        cal.record(0.995, True)
    cal.record(0.995, False)
    eng = StrategyEngine(cfg, risk, MidModel(), cal)
    rec, wt = FakeRec(), FakeTwap()
    # 进场价 0.80 < 0.95 → 闸门强制开启：cal≈0.68 < 0.85+0.001 → 拒绝
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.80"))
    d = eng.decide(features(up_ask=0.85, up_bid=0.84), rec, wt)
    assert d.action == "NOOP", d
    # 同样的模型、进场价 0.98（≥0.95，闸门不强制）→ 纯价格闸门放行（原行为）
    eng2 = StrategyEngine(cfg, risk, MidModel(), cal)
    d2 = eng2.decide(features(up_ask=0.99, up_bid=0.989), rec, wt)
    assert d2.action == "TAIL_CAPTURE_UP", d2


def test_tail_exit_fires_at_target():
    """出场价 0.99：持仓方 bid ≥ 0.99 → FAK 卖出落袋（替代持有到结算）。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.90"), Decimal("0.99"))
    # 模拟尾仓：0.90 买入 5.56 股
    eng.on_fill(7, "UP", Decimal("5.56"), Decimal("0.90"), "BUY", {"module": "tail_capture"})
    # bid=0.99 到出场价 → 出场
    d = eng.decide(features(up_bid=0.99, up_ask=0.995, remaining_s=60.0), rec, wt)
    assert d.action == "EXIT_UP", d
    assert d.side == "SELL" and d.tif == "FAK" and d.post_only is False
    assert Decimal(d.price) == Decimal("0.99")
    assert Decimal(d.qty) == Decimal("5.56")


def test_tail_exit_below_target_holds():
    """出场价未到 → 继续持有（原行为）。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.90"), Decimal("0.99"))
    eng.on_fill(7, "UP", Decimal("5.56"), Decimal("0.90"), "BUY", {"module": "tail_capture"})
    # 对侧约 10¢（0.90 进场的真实盘口）：对冲不划算 → 未到出场价时持有
    d = eng.decide(features(up_bid=0.97, up_ask=0.975, down_bid=0.09, down_ask=0.10,
                            remaining_s=60.0), rec, wt)
    assert d.action == "NOOP", d


def test_tail_exit_disabled_by_default():
    """默认出场价 0（关闭）→ 持有到结算，不触发中途出场。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.on_fill(7, "UP", Decimal("5.56"), Decimal("0.90"), "BUY", {"module": "tail_capture"})
    d = eng.decide(features(up_bid=0.995, up_ask=0.999, down_bid=0.09, down_ask=0.10,
                            remaining_s=60.0), rec, wt)
    assert d.action == "NOOP", d


def test_tail_exit_clears_position_lock():
    """尾仓出场成交后释放按资产锁，下一窗口可重新入场。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.90"), Decimal("0.99"))
    eng.on_fill(7, "UP", Decimal("5.56"), Decimal("0.90"), "BUY", {"module": "tail_capture"})
    d = eng.decide(features(up_bid=0.99, up_ask=0.995, remaining_s=60.0), rec, wt)
    assert d.action == "EXIT_UP"
    # 出场成交 → 清仓
    eng.on_fill(7, "UP", Decimal("5.56"), Decimal("0.99"), "SELL", {"module": "exit"})
    pos = eng.positions[7]
    assert pos.tail_held is False and pos.up_qty == 0
    assert 7 not in eng._active_tail_market
    # 下一窗口（market 8，同资产）可重新入场
    rec2 = FakeRec()
    rec2.market_id = 8
    d2 = eng.decide(features(), rec2, wt)
    assert d2.action == "TAIL_CAPTURE_UP", d2


def test_tail_auto_hedge_disabled():
    """auto_hedge=false（默认）：尾仓即使对冲划算（90+1=91¢ 锁 9%）也不触发对冲。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.on_fill(7, "UP", Decimal("5.56"), Decimal("0.90"), "BUY", {"module": "tail_capture"})
    d = eng.decide(features(up_bid=0.89, up_ask=0.90, down_bid=0.009, down_ask=0.010,
                            remaining_s=60.0), rec, wt)
    assert d.action == "NOOP", d


def test_entry_delay_blocks_early_entry():
    """延迟进场：第 3 分钟前禁止开新仓，到达后放行；默认关闭时不受影响。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    # 开启延迟：第 3 分钟（into=180s）后才可进场
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.98"), None, True, 3)
    d = eng.decide(features(into_window_s=120.0), rec, wt)
    assert d.action == "NOOP", d
    d = eng.decide(features(into_window_s=180.0), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP", d


def test_entry_delay_disabled_by_default():
    """默认关闭：窗口一开始就可进场（无延迟）。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    d = eng.decide(features(into_window_s=30.0), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP", d


def test_tail_entry_mode_market_taker():
    """进场方式=市价：TAIL_CAPTURE 决策 tif=FAK、post_only=False、价=ask（保证成交）。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.90"), None, False, 0, "market")
    d = eng.decide(features(up_ask=0.92, up_bid=0.91), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP", d
    assert d.tif == "FAK" and d.post_only is False
    assert Decimal(d.price) == Decimal("0.92")   # 市价 = ask


def test_tail_entry_mode_limit_default():
    """默认限价：TAIL_CAPTURE 仍为 GTD post-only 挂单（价 = bid+1tick 截限价）。"""
    cfg = Config()
    cfg.strategy["entry"]["entry_min_gross_edge"] = 99
    eng = make_engine(cfg)
    rec, wt = FakeRec(), FakeTwap()
    eng.set_runtime_controls(True, Decimal("5"), Decimal("0.98"), None, False, 0, "limit")
    d = eng.decide(features(up_ask=0.99, up_bid=0.989), rec, wt)
    assert d.action == "TAIL_CAPTURE_UP", d
    assert d.tif == "GTD" and d.post_only is True
