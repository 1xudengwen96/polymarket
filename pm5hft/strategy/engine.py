"""策略引擎（docs/04）：状态机 + 六个模块。

目标：「便宜筹码 → 重定价或低成本对冲 → 退出/锁定」。
铁律在 RiskEngine 硬编码，本引擎遵循：同向不加仓、亏钱不对冲、不强留等对冲。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation

from ..config import Config
from ..logging_setup import get_logger
from ..probability.calibration import Calibrator
from ..probability.edge import compute_edge
from ..probability.model import Model
from ..risk import PreTradeContext, RiskEngine

# 状态机（docs/04 §1）
S0_NO_POSITION = "S0_NO_POSITION"
S1_SEEKING = "S1_SEEKING"
S2_SMALL_INITIAL = "S2_SMALL_INITIAL"
S3_WAIT_REPRICING = "S3_WAIT_REPRICING"
S4A_EXIT_PROFIT = "S4A_EXIT_PROFIT"
S4B_HEDGE = "S4B_HEDGE"
S5_COMPLETE_SET = "S5_COMPLETE_SET"
S6_LOCKED_PROFIT = "S6_LOCKED_PROFIT"
S7_SETTLEMENT = "S7_SETTLEMENT"


@dataclass
class PositionState:
    market_id: int
    asset: str
    state: str = S0_NO_POSITION
    up_qty: Decimal = Decimal("0")
    down_qty: Decimal = Decimal("0")
    avg_up: Decimal | None = None
    avg_down: Decimal | None = None
    entries_up: int = 0
    entries_down: int = 0
    entry_ts_ms: int = 0
    tail_held: bool = False  # 尾部持仓（免止损，持有到结算）
    tail_pending: bool = False  # 尾部挂单已发出（防每秒重发）
    exit_pending: bool = False  # maker 出场挂单已发出（防每秒重发）
    fade_held: bool = False  # xrp_fade 实验持仓（免止损/免出场，持有到结算）
    fade_pending: bool = False  # xrp_fade 挂单已发出（防每秒重发）
    mid_held: bool = False  # mid_capture 实验持仓（免止损/免对冲，持有到结算）
    mid_pending: bool = False  # mid_capture 挂单已发出（防每秒重发）
    mid_exit: str = "hold"  # 出场变体：hold=持有到结算 | profit3=+3tick 出场（A/B 对照）


@dataclass
class Decision:
    action: str  # NOOP|ENTER_UP|ENTER_DOWN|HEDGE_DOWN|HEDGE_UP|EXIT_PROFIT|EXIT_STOP|
    # EXIT_SIGNAL|TAIL_CAPTURE_UP|TAIL_CAPTURE_DOWN|TAIL_HEDGE|ARB|CANCEL
    side: str | None = None        # BUY | SELL
    token_side: str | None = None  # UP | DOWN
    price: str | None = None       # Decimal 字符串
    qty: str | None = None
    tif: str | None = None         # GTC|GTD|FOK|FAK
    post_only: bool = True
    reason: str | None = None
    reject_code: str | None = None
    exit_mode: str | None = None   # mid_capture 出场变体（hold|profit3），dispatch 写入订单 meta

    @staticmethod
    def noop(reason: str | None = None, reject_code: str | None = None) -> Decision:
        return Decision(action="NOOP", reason=reason, reject_code=reject_code)


def effective_tick(price: Decimal, base_tick: str = "0.01") -> Decimal:
    """真实市场 tick 规则：价格 ≥0.96 或 <0.04 时 tick 为 0.001，其余 0.01。

    注意：不信任 gamma 的 orderPriceMinTickSize——它反映的是窗口关闭后的
    终态 tick（价格曾穿越 0.96 时历史窗口会被标成 0.001），会造成全程误判。
    """
    if price >= Decimal("0.96") or price < Decimal("0.04"):
        return Decimal("0.001")
    return Decimal("0.01")


def side_tick(f: dict, token_side: str, price: Decimal, base_tick: str = "0.01") -> Decimal:
    """下单侧 tick：优先用交易所实时 tick（tick_size_change 事件写入，
    反映交易所当前强制校验的 tick），无实时数据时回退价格区域规则。

    实盘实证（2026-08-17）：
    - 价格刚进入 ≥0.96 尾区时，交易所 tick 翻转可能滞后数分钟；区域规则
      会挂出 3 位小数价被成批拒绝（0.981 → "minimum tick size rule 0.01"）。
    - tick_size_change 事件可靠且持久（曾因 book 快照的 tick_size 字段
      覆盖事件值导致失真——该快照字段已弃用，不再干扰）。
    - 用实时 tick 挂单：翻转前 2 位小数（0.98/0.99，永远合法），翻转后
      3 位小数（0.981，最优队列位）——始终匹配交易所当前强制校验。
    """
    raw = f.get("up_tick" if token_side == "UP" else "down_tick")
    if raw:
        try:
            return Decimal(str(raw))
        except InvalidOperation:  # noqa: BLE001
            pass
    return effective_tick(price, base_tick)


class StrategyEngine:
    def __init__(self, config: Config, risk: RiskEngine, model: Model, calibrator: Calibrator) -> None:
        self.config = config
        self.risk = risk
        self.model = model
        self.calibrator = calibrator
        self.log = get_logger("strategy")
        self.positions: dict[int, PositionState] = {}
        self._active_tail_market: dict[str, int] = {}  # 按资产的尾部持仓锁（每资产同时仅一仓）
        self._active_fade_market: dict[str, int] = {}  # 按资产的反向实验持仓锁
        self._active_mid_market: dict[str, int] = {}  # 按资产的中段实验持仓锁
        self._last_day: int | None = None
        self._last_hour: int | None = None
        self.auto_trading_enabled = True
        self.fixed_order_notional = Decimal(str(config.s("tail_capture.fixed_order_notional", 5)))
        # 尾盘入场最低 ask 价（Dashboard 可运行时覆盖；默认 0.98 = 原行为）
        self.tail_entry_price = Decimal(str(config.s("tail_capture.entry_price_min", 0.98)))
        # 尾盘止盈出场价（0 = 关闭，持有到结算；>0 = 持仓方 bid ≥ 该价即卖出落袋）
        self.tail_exit_price = Decimal(str(config.s("tail_capture.exit_price", 0)))
        # 反向实验独立风控实例：实验盈亏/连亏/熔断与主账本完全隔离
        # （冷门方胜率极低，若共享风控会以连亏冷却/日亏限额污染主策略）
        self.fade_assets: list[str] = [
            str(a) for a in (config.s("xrp_fade.assets", []) or [])
        ] if config.s("xrp_fade.enabled", False) else []
        self.fade_risk: RiskEngine | None = None
        if self.fade_assets:
            ov = dict(config.s("xrp_fade.risk_overrides", {}) or {})
            self.fade_risk = RiskEngine(config, mode=risk.mode, overrides=ov)
            self.log.info("xrp fade experiment enabled", assets=self.fade_assets, overrides=ov)
        # 中段买方实验（与主 tail 同资产并行；同窗口两模块互斥、独立风控账本）
        self.mid_assets: list[str] = [
            str(a) for a in (config.s("mid_capture.assets", []) or [])
        ] if config.s("mid_capture.enabled", False) else []
        self.mid_risk: RiskEngine | None = None
        if self.mid_assets:
            ov = dict(config.s("mid_capture.risk_overrides", {}) or {})
            self.mid_risk = RiskEngine(config, mode=risk.mode, overrides=ov)
            self.log.info("mid capture experiment enabled", assets=self.mid_assets, overrides=ov)

    # ── 主入口 ───────────────────────────────────────────────
    def set_runtime_controls(self, enabled: bool, fixed_order_notional: Decimal,
                             tail_entry_price: Decimal | None = None,
                             tail_exit_price: Decimal | None = None) -> None:
        self.auto_trading_enabled = enabled
        self.fixed_order_notional = fixed_order_notional
        if tail_entry_price is not None:
            self.tail_entry_price = max(Decimal("0.50"), min(Decimal("0.999"), tail_entry_price))
        if tail_exit_price is not None:
            self.tail_exit_price = max(Decimal("0"), min(Decimal("0.999"), tail_exit_price))

    def decide(self, f: dict, rec, wt) -> Decision:
        """f: FeatureStore.features() 输出；rec: MarketRecord；wt: WindowTwap。"""
        pos = self.positions.setdefault(rec.market_id, PositionState(rec.market_id, rec.asset))
        self._maybe_reset_periodic(f.get("ts_ms"))

        # 结算/重置（Phase 2 由 supervisor 调用 on_settlement）
        remaining = float(f.get("remaining_s") or 0.0)
        into = float(f.get("into_window_s") or 0.0)
        ptb_ready = wt is not None and wt.ptb is not None and wt.self_result is None

        # 可选尾部止盈出场：出场价 > 0 时，持仓方 bid ≥ 出场价 → 卖出落袋。
        # 放在 PTB 闸门之前（出场只需盘口，无需 PTB；且自动交易关闭时也要能出场）。
        if pos.tail_held and self.tail_exit_price > 0:
            d = self._tail_exit(f, rec, pos, remaining)
            if d.action != "NOOP":
                return d

        if not ptb_ready:
            return Decision.noop("PTB not ready", "PTB_MISSING")

        if not self.auto_trading_enabled:
            # 停止自动交易只阻止新仓。已有普通仓位仍可按状态机退出/对冲，
            # 尾部仓位继续持有到结算。
            if pos.up_qty > 0 or pos.down_qty > 0:
                return self._state_machine(f, rec, wt, pos, remaining, into)
            return Decision.noop("auto trading disabled", "AUTO_TRADING_DISABLED")

        # 反向实验资产：主策略（状态机/tail/arb）一律跳过，只跑 xrp_fade
        if self.fade_assets and rec.asset in self.fade_assets:
            return self._xrp_fade(f, rec, pos, remaining)

        # 主循环决策（状态机）
        d = self._state_machine(f, rec, wt, pos, remaining, into)
        if d.action != "NOOP":
            return d

        # 独立模块：TAIL_CAPTURE / TAIL_HEDGE / ARB（有时间与风险守卫）
        if self.config.s("tail_capture.enabled", False):
            d2 = self._tail_capture(f, rec, wt, pos, remaining)
            if d2.action != "NOOP":
                return d2
            # 方向性入场已禁用时，状态机的拒绝原因无信息量；
            # 决策流保留尾部模块的 NOOP 原因更有可解释性
            if d.reason in ("gross edge below threshold", "directional entry disabled") or d2.reject_code:
                d = d2
        if self.config.s("arb.enabled", False):
            d2 = self._arb(f, rec, wt, remaining)
            if d2.action != "NOOP":
                return d2
        # 中段买方实验（tail 之后评估 → 主 tail 优先；同窗口互斥在模块内守卫）
        if self.mid_assets and rec.asset in self.mid_assets:
            d2 = self._mid_capture(f, rec, pos, remaining)
            if d2.action != "NOOP":
                return d2
        # 保留状态机的具体 NOOP 原因（复盘可解释性）
        return d

    # ── 状态机 ───────────────────────────────────────────────
    def _state_machine(self, f: dict, rec, wt, pos: PositionState, remaining: float, into: float) -> Decision:
        up_ask = f.get("up_ask")
        f.get("down_ask")
        up_bid = f.get("up_bid")

        # 概率与 Edge
        raw_p = self.model.predict(f)
        cal_p = self.calibrator.calibrate(raw_p, market_price=None).cal_prob
        market_p = (float(up_bid) + float(up_ask)) / 2.0 if up_bid is not None and up_ask is not None else None
        taker_fee = self._taker_fee(rec)
        s = self.config.s
        model_err = s("entry.model_err", self.config.p("model_err_cold_start", 0.03))
        risk_buffer = s("entry.risk_buffer", self.config.p("risk_buffer_global", 0.005))

        if pos.state == S0_NO_POSITION:
            if up_ask is None:
                return Decision.noop("no book", "NO_BOOK")
            # 方向性入场硬禁用：阈值=99 时无条件拒绝（防止单边盘口等路径绕过）
            if s("entry.entry_min_gross_edge", 0.06) >= 99:
                return Decision.noop("directional entry disabled", "ENTRY_DISABLED")
            if into < s("entry.entry_min_age_s", 5):
                return Decision.noop("too early in window")
            if remaining < s("entry.entry_min_remaining_s", 30):
                return Decision.noop("too close to close")
            # 时间上限守卫（T-60 专用变体：entry_max_remaining_s > 0 时仅限时窗口内入场）
            max_rem = s("entry.entry_max_remaining_s", 0)
            if max_rem and remaining > max_rem:
                return Decision.noop("not in entry window")
            edge = compute_edge(
                cal_p, float(up_ask), taker_fee, remaining, model_err, risk_buffer,
                slippage_est=s("entry.slippage_est", 0.01),
                exec_risk=self.config.p("exec_risk_default", 0.003),
                time_risk_k=self.config.p("time_risk_k", 0.002),
            )
            # 单边盘口时用 ask 自身作市场参考，禁用阈值同样生效
            gross_ref = market_p if market_p is not None else float(up_ask)
            if cal_p - gross_ref < s("entry.entry_min_gross_edge", 0.06):
                return Decision.noop("gross edge below threshold")
            if edge.net_edge < s("entry.entry_min_net_edge", 0.04):
                return Decision.noop("net edge below threshold", "EDGE_BELOW")
            # 做多 Up（DOWN 方向对称：由市场给出 Down 便宜价时触发，此处以 Up 为例，
            # 对称逻辑在 _entry_candidate 中处理）
            return self._entry_candidate(f, rec, pos, "UP", cal_p, edge, remaining)

        if pos.state == S1_SEEKING:
            # 已发出建仓单（等待成交/过期）；执行层成交或取消后回写状态
            return Decision.noop("seeking: order pending")

        if pos.state in (S2_SMALL_INITIAL, S3_WAIT_REPRICING, S4B_HEDGE):
            # 中段实验持仓：hold 变体持有到结算；profit3 变体走 +3tick 出场
            # （价格过 0.96 后 tick=0.001，出场线几乎贴成本——冲刺即 FAK 落袋），
            # 未达利润线仍持有到结算（无止损/时间止损，吃结算 EV）
            if pos.mid_held:
                if pos.mid_exit == "profit3":
                    held = "UP" if pos.up_qty > 0 else "DOWN"
                    exit_d = self._exit_candidate(f, rec, pos, held, cal_p, remaining)
                    if exit_d.action != "NOOP":
                        return exit_d
                return Decision.noop("mid: hold to settlement")
            # 持仓 Up 分支（Down 持仓对称处理见 _exit_candidate）
            if pos.up_qty > 0 and pos.down_qty == 0:
                exit_d = self._exit_candidate(f, rec, pos, "UP", cal_p, remaining)
                if exit_d.action != "NOOP":
                    return exit_d
                hedge_d = self._hedge_candidate(f, rec, pos, "UP", remaining)
                if hedge_d.action != "NOOP":
                    return hedge_d
            elif pos.down_qty > 0 and pos.up_qty == 0:
                exit_d = self._exit_candidate(f, rec, pos, "DOWN", cal_p, remaining)
                if exit_d.action != "NOOP":
                    return exit_d
                hedge_d = self._hedge_candidate(f, rec, pos, "DOWN", remaining)
                if hedge_d.action != "NOOP":
                    return hedge_d
            return Decision.noop("holding: waiting for repricing or hedge")

        if pos.state in (S5_COMPLETE_SET, S6_LOCKED_PROFIT):
            return self._locked_handle(f, rec, pos, remaining)

        return Decision.noop(f"state {pos.state}")

    # ── 模块一：Cheap Entry ──────────────────────────────────
    def _entry_candidate(self, f: dict, rec, pos: PositionState, direction: str, cal_p: float,
                         edge, remaining: float) -> Decision:
        if direction == "UP":
            price = Decimal(str(f["up_ask"]))
            token_side = "UP"
        else:
            price = Decimal(str(f["down_ask"]))
            token_side = "DOWN"
        notional = self._initial_notional()
        qty = self._round_qty(notional / price, rec)
        if qty < Decimal(rec.min_order_size or "5"):
            return Decision.noop("qty below min size")
        ctx = PreTradeContext(
            market_id=rec.market_id, asset=rec.asset, kind="entry", side="BUY",
            token_side=token_side, qty=qty, price=price, taker_fee=self._taker_fee(rec),
            remaining_s=remaining, ptb_ready=True, entry_direction=direction,
        )
        check = self.risk.pre_trade(ctx)
        if not check.ok:
            return Decision.noop(check.reason, check.blocked_code)
        # maker 优先：挂 post-only 限价单。
        # 默认价格 = ask-1tick；maker_fair_margin > 0 时挂模型公允价下方（吃价差变体）
        tick = side_tick(f, token_side, price, rec.tick_size)
        fair_margin = self.config.s("entry.maker_fair_margin", 0.0)
        if fair_margin and fair_margin > 0:
            maker_price = (Decimal(str(cal_p)) - Decimal(str(fair_margin))).quantize(tick)
            # post-only：不得穿越簿口
            if maker_price >= price:
                maker_price = (price - tick).quantize(tick)
        else:
            maker_price = (price - tick).quantize(tick)
        if maker_price <= 0:
            return Decision.noop("maker price invalid")
        pos.state = S1_SEEKING  # 决策发出 → 等待执行层成交/过期回写
        return Decision(
            action=f"ENTER_{token_side}", side="BUY", token_side=token_side,
            price=str(maker_price), qty=str(qty), tif="GTD", post_only=True,
            reason=f"net_edge={edge.net_edge:.4f} cal_p={cal_p:.4f} ask={price}",
        )

    def _initial_notional(self) -> Decimal:
        s = self.config.s
        equity = self.risk.equity
        n = equity * Decimal(str(s("entry.initial_position_pct", 0.002)))
        lo = Decimal(str(s("entry.min_initial_notional", 5)))
        hi = Decimal(str(s("entry.max_initial_notional", 50)))
        return max(lo, min(hi, n))

    def _round_qty(self, qty: Decimal, rec) -> Decimal:
        step = Decimal(rec.min_order_size or "5")
        return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _qty_for_fixed_notional(notional: Decimal, price: Decimal, rec) -> Decimal:
        """Return two-decimal shares whose order value is at least ``notional``.

        ``min_order_size`` is a minimum share count, not a quantity increment.
        Rounding down here can turn a requested 5 USDC order into 4.90 USDC at
        a 0.98 price, so fixed-notional entries must round shares upward.
        """
        min_shares = Decimal(rec.min_order_size or "5")
        qty = (notional / price).quantize(Decimal("0.01"), rounding=ROUND_UP)
        return max(min_shares, qty)

    # ── 模块二/三：退出与对冲 ────────────────────────────────
    def _exit_candidate(self, f: dict, rec, pos: PositionState, held: str, cal_p: float,
                        remaining: float) -> Decision:
        # 尾部持仓：永不中途出场（显式封死，置于一切出场逻辑之前）。
        # 数据结论（纸面 369 笔反事实）：持有到结算 -4.95 优于所有出场变体
        # （+1/+3tick 锁定 -12.85，末段出场 -11.95~-15.60，冲顶出场中性但救 0
        # 只天鹅）。原因：赢单利润在结算端（0.981→1.00），中途可成交价最高
        # ~0.99，出场必然割肉给赢单；而 8/8 天鹅崩盘前最高买价 ≤0.99，
        # 利润锁定类出场结构上抓不住天鹅。天鹅风险由入场端（校准桶禁用/
        # 周末闸门/小仓位）管理，不由出场管理。
        if pos.tail_held:
            return Decision.noop("tail: hold to settlement")
        s = self.config.s
        if held == "UP":
            bid = f.get("up_bid")
            avg = pos.avg_up
            token_side, _opp_side = "UP", "DOWN"
        else:
            bid = f.get("down_bid")
            avg = pos.avg_down
            token_side, _opp_side = "DOWN", "UP"
        if bid is None or avg is None:
            return Decision.noop("no exit book")
        bid_d = Decimal(str(bid))
        tick = side_tick(f, token_side, bid_d, rec.tick_size)
        profit_ticks = (bid_d - avg) / tick
        if profit_ticks >= s("exit.exit_min_profit_ticks", 3):
            # maker 出场变体：挂 entry+1tick post-only 而非市价吃单（exit_mode=maker）
            if s("exit.exit_mode", "taker") == "maker":
                target = (avg + tick * Decimal(str(s("exit.exit_min_profit_ticks", 3)))).quantize(tick)
                if target < bid_d:  # post-only 不得穿越簿口 → 走 taker 兜底
                    return Decision(action="EXIT_PROFIT", side="SELL", token_side=token_side,
                                    price=str(bid_d), qty=str(pos.up_qty if held == "UP" else pos.down_qty),
                                    tif="FAK", post_only=False, reason=f"profit {profit_ticks} ticks")
                if pos.exit_pending:  # 挂单已发出：等待成交/过期，不重发
                    return Decision.noop("maker exit pending")
                pos.exit_pending = True
                return Decision(action="EXIT_PROFIT", side="SELL", token_side=token_side,
                                price=str(target), qty=str(pos.up_qty if held == "UP" else pos.down_qty),
                                tif="GTD", post_only=True, reason=f"maker exit @{target}")
            return Decision(action="EXIT_PROFIT", side="SELL", token_side=token_side,
                            price=str(bid_d), qty=str(pos.up_qty if held == "UP" else pos.down_qty),
                            tif="FAK", post_only=False, reason=f"profit {profit_ticks} ticks")
        # 反向实验持仓：持有到结算（翻盘收益在结算端，任何中途出场都会截断）
        if pos.fade_held:
            return Decision.noop("xrp fade: hold to settlement")
        # 中段实验持仓：持有到结算（出场截断实验口径）
        if pos.mid_held:
            return Decision.noop("mid: hold to settlement")
        if (avg - bid_d) / tick >= s("exit.stop_loss_ticks", 6):
            return Decision(action="EXIT_STOP", side="SELL", token_side=token_side,
                            price=str(bid_d), qty=str(pos.up_qty if held == "UP" else pos.down_qty),
                            tif="FAK", post_only=False, reason="stop loss")
        if remaining <= s("exit.stop_remaining_s", 15):
            return Decision(action="EXIT_STOP", side="SELL", token_side=token_side,
                            price=str(bid_d), qty=str(pos.up_qty if held == "UP" else pos.down_qty),
                            tif="FAK", post_only=False, reason="time stop")
        # 模型反转离场
        market_now = (float(bid) + float(f.get(f"{token_side.lower()}_ask") or bid)) / 2.0
        if cal_p - market_now < -s("exit.exit_confidence_turn", 0.08):
            return Decision(action="EXIT_SIGNAL", side="SELL", token_side=token_side,
                            price=str(bid_d), qty=str(pos.up_qty if held == "UP" else pos.down_qty),
                            tif="FAK", post_only=False, reason="model reversal")
        return Decision.noop()

    def _hedge_candidate(self, f: dict, rec, pos: PositionState, held: str, remaining: float) -> Decision:
        s = self.config.s
        # 尾仓不自动对冲（auto_hedge=false 默认关闭）：纯 进场→出场/结算 流程。
        # 实盘验证：90¢ 尾仓 + 3-4¢ 对冲 = 锁 6¢，把赢单的 10¢ 利润削掉 4¢
        if pos.tail_held and not s("tail_capture.auto_hedge", False):
            return Decision.noop("tail: auto hedge disabled")
        if held == "UP":
            opp_ask = f.get("down_ask")
            avg = pos.avg_up
            opp_side = "DOWN"
            held_qty = pos.up_qty
        else:
            opp_ask = f.get("up_ask")
            avg = pos.avg_down
            opp_side = "UP"
            held_qty = pos.down_qty
        if opp_ask is None or avg is None:
            return Decision.noop("no hedge book")
        opp_ask_d = Decimal(str(opp_ask))
        complete_cost = avg + opp_ask_d
        fee = self._taker_fee(rec)
        net_locked = Decimal("1") - complete_cost - Decimal(str(2 * fee)) - Decimal(str(s("hedge.hedge_safety_margin", 0.015)))
        if net_locked < Decimal(str(s("hedge.hedge_min_locked_pct", 0.03))):
            return Decision.noop(f"hedge not profitable: net_locked={net_locked:.4f}", "HEDGE_NOT_PROFITABLE")
        qty = self._round_qty(held_qty, rec)
        if qty <= 0:
            return Decision.noop("hedge qty zero")
        ctx = PreTradeContext(
            market_id=rec.market_id, asset=rec.asset, kind="hedge", side="BUY",
            token_side=opp_side, qty=qty, price=opp_ask_d, taker_fee=fee,
            remaining_s=remaining, ptb_ready=True, complete_set_cost=complete_cost,
        )
        check = self.risk.pre_trade(ctx)
        if not check.ok:
            return Decision.noop(check.reason, check.blocked_code)
        tick = side_tick(f, opp_side, opp_ask_d, rec.tick_size)
        maker_price = (opp_ask_d - tick).quantize(tick)
        if maker_price <= 0:
            return Decision.noop("hedge maker price invalid")
        return Decision(
            action=f"HEDGE_{opp_side}", side="BUY", token_side=opp_side,
            price=str(maker_price), qty=str(qty), tif="GTD", post_only=True,
            reason=f"complete_set_cost={complete_cost} net_locked={net_locked:.4f}",
        )

    def _locked_handle(self, f: dict, rec, pos: PositionState, remaining: float) -> Decision:
        s = self.config.s
        min_price = Decimal(str(s("hedge.settle_sell_min_price", 0.995)))
        up_bid = f.get("up_bid")
        down_bid = f.get("down_bid")
        if up_bid is not None and down_bid is not None:
            if Decimal(str(up_bid)) >= min_price and Decimal(str(down_bid)) >= min_price:
                return Decision(action="CANCEL", reason="locked: both bids above settle sell threshold",
                                reject_code=None)
        return Decision.noop("locked profit: hold to settlement")

    # ── 模块四/五/六 ─────────────────────────────────────────
    def _tail_exit(self, f: dict, rec, pos: PositionState, remaining: float) -> Decision:
        """可选尾部止盈出场：持仓方 bid ≥ 出场价 → FAK 卖出落袋（替代持有到结算）。

        出场价 = 0 时不启用（默认：持有到结算，数据结论见 _exit_candidate 注释）。
        """
        if pos.up_qty > 0:
            bid = f.get("up_bid")
            token_side, qty = "UP", pos.up_qty
        elif pos.down_qty > 0:
            bid = f.get("down_bid")
            token_side, qty = "DOWN", pos.down_qty
        else:
            return Decision.noop("tail exit: no position")
        if bid is None:
            return Decision.noop("tail exit: no book")
        bid_d = Decimal(str(bid))
        if bid_d < self.tail_exit_price:
            return Decision.noop(f"tail exit: bid {bid_d} < target {self.tail_exit_price}")
        # 已到出场价：taker FAK 卖出（价 = 当前 bid，≥ 出场价），落袋离场
        return Decision(
            action=f"EXIT_{token_side}", side="SELL", token_side=token_side,
            price=str(bid_d), qty=str(qty), tif="FAK", post_only=False,
            reason=f"tail exit @{bid_d} (target {self.tail_exit_price})",
        )

    def _tail_capture(self, f: dict, rec, wt, pos: PositionState, remaining: float) -> Decision:
        """尾部买方策略（研究实测：98-99.9¢ 实际胜率 99.2-99.8%，平均 ROI +0.65%/笔）。

        maker 挂单（post-only）+ 持有到结算；不设止损（尾部策略吃结算 EV）。
        """
        s = self.config.s
        if remaining < s("tail_capture.tail_min_remaining_s", 10):
            return Decision.noop("tail capture: too late")
        # 单窗口单挂单 + 按资产单持仓（防同资产仓位堆叠；跨资产互不阻塞，
        # 组合敞口由风控层 max_unhedged_exposure_pct 兜底）
        if pos.tail_held or pos.tail_pending:
            return Decision.noop("tail capture: already active")
        # 中段实验互斥：同窗口 mid 已占（挂单/持仓）或同资产 mid 持仓在途 → tail 让位
        if pos.mid_held or pos.mid_pending or rec.asset in self._active_mid_market:
            return Decision.noop("tail capture: mid experiment active")
        if rec.asset in self._active_tail_market and self._active_tail_market[rec.asset] != rec.market_id:
            return Decision.noop("tail capture: another window active")
        # 限价语义：进场价 = 最高可接受买价。挂单价不高于它（市场价高于它就不成交）。
        limit = self.tail_entry_price
        # 低价位安全阀：进场价低于阈值时强制模型闸门（纯价格闸门在低价位会买贵：
        # 实盘案例 use_model_gate=false + 门槛 0.80 → 74% 校准概率花 84¢ 成交的负 EV 单）
        gate = bool(s("tail_capture.use_model_gate", True)) \
            or float(limit) < float(s("tail_capture.low_price_gate_threshold", 0.95))
        for ask_key, bid_key, token_side in (("up_ask", "up_bid", "UP"), ("down_ask", "down_bid", "DOWN")):
            ask = f.get(ask_key)
            bid = f.get(bid_key)
            if ask is None:
                continue
            price = float(ask)
            if not (float(limit) <= price < 0.999):
                continue
            # 簿口新鲜度熔断（risk.yaml breakers.book_stale_ms）：
            # 语义 = 该侧簿口多久没收到更新（到达时刻计，防订阅丢失/断流），
            # 达到阈值禁止入场（纸面/实盘同守卫）。
            # 注意：CLOB 流自身有 1-17s 传输延迟，但那是全市场共有的信息延迟，
            # 不构成熔断理由；簿口值仍按流内权威价校准。
            max_book_age = float(self.config.risk.breakers.get("book_stale_ms", 1500))
            age_key = "up_book_age_ms" if token_side == "UP" else "down_book_age_ms"
            book_age = f.get(age_key)
            if book_age is not None and book_age > max_book_age:
                return Decision.noop(f"tail capture: book stale ({book_age:.0f}ms)", "BOOK_STALE")
            raw_p = self.model.predict(f)
            if token_side == "DOWN":
                raw_p = 1.0 - raw_p
            cal = self.calibrator.calibrate(raw_p, market_price=price)
            if cal.tail_capture_unsafe:
                continue  # 99¢ 桶实际胜率不足 → 该侧禁用（校准数据，独立于模型闸门）
            buffer = s("tail_capture.buffer", 0.003) + s("tail_capture.tail_risk_buffer", 0.001)
            # 模型闸门：校准概率 ≥ 价格+buffer 才买（低价位安全阀强制开启）。
            # use_model_gate=false 且进场价 ≥ 阈值时跳过（纯价格闸门）——用于模型
            # 特征漂移期的纸面过渡，依据：静态 EV 无过滤 +0.65%/笔；纸面过夜 17/17
            # 触碰全胜而模型全部误拒。
            if gate and cal.cal_prob < price + buffer:
                continue
            # maker 挂单价：bid+1tick（post-only，不吃价差），且不高于用户限价
            tick = side_tick(f, token_side, Decimal(str(price)), rec.tick_size)
            maker_price = Decimal(str(price))
            if bid is not None:
                maker_price = (Decimal(str(bid)) + tick).quantize(tick)
            maker_price = min(maker_price, (Decimal(str(price)) - tick).quantize(tick))
            maker_price = min(maker_price, limit)
            if maker_price <= 0:
                continue
            max_notional = Decimal(str(s("tail_capture.max_tail_notional", 100)))
            notional = min(self.fixed_order_notional, max_notional)
            qty = self._qty_for_fixed_notional(notional, maker_price, rec)
            ctx = PreTradeContext(
                market_id=rec.market_id, asset=rec.asset, kind="tail_capture", side="BUY",
                token_side=token_side, qty=qty, price=maker_price,
                taker_fee=self._taker_fee(rec), remaining_s=remaining, ptb_ready=True,
                bypass_account_limits=True,
            )
            check = self.risk.pre_trade(ctx)
            if not check.ok:
                return Decision.noop(f"tail capture: {check.reason}", check.blocked_code)
            pos.tail_pending = True
            return Decision(
                action=f"TAIL_CAPTURE_{token_side}", side="BUY", token_side=token_side,
                price=str(maker_price), qty=str(qty), tif="GTD", post_only=True,
                reason=f"tail ev: cal={cal.cal_prob:.4f} ask={price:.4f} post={maker_price}",
            )
        return Decision.noop("tail capture: no candidate")

    def _xrp_fade(self, f: dict, rec, pos: PositionState, remaining: float) -> Decision:
        """反向实验（独立账本）：热门侧 ask∈[0.98,0.999) 时买对侧冷门方（≤2¢），持有到结算。

        样本内镜像 XRP 6 笔黑天鹅约 +14.4 USDC/5 股，盈亏平衡翻盘率仅 2%；
        实验目的：实测冷门方 maker 成交率 + 真实翻盘率。maker 未成交零成本。
        挂单/持仓与主 tail 铁律互不干扰（独立锁、独立风控、独立 module 标记）。
        """
        s = self.config.s
        if remaining < s("tail_capture.tail_min_remaining_s", 10):
            return Decision.noop("xrp fade: too late")
        if pos.fade_held or pos.fade_pending:
            return Decision.noop("xrp fade: already active")
        if rec.asset in self._active_fade_market and self._active_fade_market[rec.asset] != rec.market_id:
            return Decision.noop("xrp fade: another window active")
        max_px = Decimal(str(s("xrp_fade.max_longshot_price", 0.02)))
        mode = str(s("xrp_fade.mode", "maker"))
        for fav_key, long_ask_key, long_bid_key, long_side in (
            ("up_ask", "down_ask", "down_bid", "DOWN"),
            ("down_ask", "up_ask", "up_bid", "UP"),
        ):
            fav = f.get(fav_key)
            long_ask = f.get(long_ask_key)
            long_bid = f.get(long_bid_key)
            if fav is None or long_ask is None:
                continue
            if not (0.98 <= float(fav) < 0.999):
                continue
            if Decimal(str(long_ask)) > max_px:
                continue
            if mode == "taker":
                # FAK 直接吃 ask：翻盘是向上跳，maker 挂单在下方结构性吃不到（实测 0% 成交）
                fill_price = Decimal(str(long_ask))
                qty = self._round_qty(Decimal(str(s("xrp_fade.position_shares", 5))), rec)
                tif, post_only = "FAK", False
            else:
                # maker 挂单：冷门方 bid+1tick（post-only，不吃价差；未成交零成本）
                tick = side_tick(f, long_side, Decimal(str(long_ask)), rec.tick_size)
                fill_price = (Decimal(str(long_ask)) - tick).quantize(tick)
                if long_bid is not None:
                    fill_price = min((Decimal(str(long_bid)) + tick).quantize(tick), fill_price)
                if fill_price <= 0:
                    continue
                notional = Decimal(str(s("xrp_fade.position_notional", 5)))
                qty = self._round_qty(notional / fill_price, rec)
                tif, post_only = "GTD", True
            if qty <= 0:
                continue
            ctx = PreTradeContext(
                market_id=rec.market_id, asset=rec.asset, kind="tail_capture", side="BUY",
                token_side=long_side, qty=qty, price=fill_price,
                taker_fee=self._taker_fee(rec), remaining_s=remaining, ptb_ready=True,
                bypass_account_limits=True,
            )
            check = (self.fade_risk or self.risk).pre_trade(ctx)
            if not check.ok:
                return Decision.noop(f"xrp fade blocked: {check.reason}", check.blocked_code)
            pos.fade_pending = True
            return Decision(
                action=f"XRP_FADE_{long_side}", side="BUY", token_side=long_side,
                price=str(fill_price), qty=str(qty), tif=tif, post_only=post_only,
                reason=f"xrp fade({mode}): fav={float(fav):.4f} long_ask={Decimal(str(long_ask))} fill={fill_price}",
            )
        return Decision.noop("xrp fade: no candidate")

    def _mid_capture(self, f: dict, rec, pos: PositionState, remaining: float) -> Decision:
        """中段买方实验（独立账本）：ask∈[0.94,0.96) 买热门侧，maker 挂 bid+1tick，持有到结算。

        依据：BTC 8/14-15 该桶 100% 胜率（回测 70 + 纸面 12 样本，邻桶均负）。
        与主 tail 同资产并行：同窗口互斥（tail 先评估优先，mid 守卫 tail 状态，
        tail 守卫 mid 状态）；挂单 30s 未成交自动过期重评，防长期阻塞主 tail。
        """
        s = self.config.s
        if remaining < s("tail_capture.tail_min_remaining_s", 10):
            return Decision.noop("mid capture: too late")
        if remaining > s("mid_capture.entry_max_remaining_s", 60):
            return Decision.noop("mid capture: before entry window")
        if pos.mid_held or pos.mid_pending:
            return Decision.noop("mid capture: already active")
        if pos.tail_held or pos.tail_pending or rec.asset in self._active_tail_market:
            return Decision.noop("mid capture: tail active")
        if rec.asset in self._active_mid_market and self._active_mid_market[rec.asset] != rec.market_id:
            return Decision.noop("mid capture: another window active")
        zone = s("mid_capture.zone", [0.94, 0.96]) or [0.94, 0.96]
        lo, hi = float(zone[0]), float(zone[1])
        for ask_key, bid_key, token_side in (("up_ask", "up_bid", "UP"), ("down_ask", "down_bid", "DOWN")):
            ask = f.get(ask_key)
            bid = f.get(bid_key)
            if ask is None:
                continue
            price = float(ask)
            if not (lo <= price < hi):
                continue
            # maker 挂单：bid+1tick（中段簿口深、价差 1¢，post-only 不吃价差）
            tick = side_tick(f, token_side, Decimal(str(price)), rec.tick_size)
            maker_price = (Decimal(str(price)) - tick).quantize(tick)
            if bid is not None:
                maker_price = min((Decimal(str(bid)) + tick).quantize(tick), maker_price)
            if maker_price < Decimal(str(lo)) - tick:
                continue
            notional = Decimal(str(s("mid_capture.position_notional", 5)))
            qty = self._round_qty(notional / maker_price, rec)
            if qty <= 0:
                continue
            ctx = PreTradeContext(
                market_id=rec.market_id, asset=rec.asset, kind="tail_capture", side="BUY",
                token_side=token_side, qty=qty, price=maker_price,
                taker_fee=self._taker_fee(rec), remaining_s=remaining, ptb_ready=True,
                bypass_account_limits=True,
            )
            check = (self.mid_risk or self.risk).pre_trade(ctx)
            if not check.ok:
                return Decision.noop(f"mid capture blocked: {check.reason}", check.blocked_code)
            pos.mid_pending = True
            # A/B 对照分配：ab 模式按 market_id 奇偶各半（确定性、可归属），
            # 单值模式全用该变体
            exit_mode = str(s("mid_capture.exit_mode", "hold"))
            variant = exit_mode if exit_mode in ("hold", "profit3") else (
                "profit3" if rec.market_id % 2 else "hold"
            )
            return Decision(
                action=f"MID_CAPTURE_{token_side}", side="BUY", token_side=token_side,
                price=str(maker_price), qty=str(qty), tif="GTD", post_only=True,
                exit_mode=variant,
                reason=f"mid capture({variant}): ask={price:.4f} post={maker_price} rem={remaining:.0f}s",
            )
        return Decision.noop("mid capture: no candidate")

    def _arb(self, f: dict, rec, wt, remaining: float) -> Decision:
        s = self.config.s
        up_ask = f.get("up_ask")
        down_ask = f.get("down_ask")
        if up_ask is None or down_ask is None:
            return Decision.noop("arb: no book")
        total = Decimal(str(up_ask)) + Decimal(str(down_ask))
        fee = self._taker_fee(rec)
        net = Decimal("1") - total - Decimal(str(2 * fee)) - Decimal(str(s("arb.slippage_buffer", 0.003)))
        if net < Decimal(str(s("arb.min_net_pct", 0.008))):
            return Decision.noop("arb: below threshold")
        qty = self._round_qty(
            min(Decimal(str(s("arb.max_notional", 50))) / Decimal("2") / Decimal(str(up_ask)),
                Decimal(str(s("arb.max_notional", 50))) / Decimal("2") / Decimal(str(down_ask))),
            rec,
        )
        if qty < Decimal(str(s("arb.min_qty", 10))):
            return Decision.noop("arb: qty below min")
        # 两腿同价不同 token；执行层负责双提交 + 单腿失败秒撤
        return Decision(action="ARB", side="BUY", token_side="BOTH",
                        price=f"{up_ask}/{down_ask}", qty=str(qty), tif="FOK", post_only=False,
                        reason=f"arb net={net:.4f} total={total}")

    # ── 记账 ─────────────────────────────────────────────────
    def on_fill(self, market_id: int, token_side: str, qty: Decimal, price: Decimal,
                side: str = "BUY", meta: dict | None = None) -> None:
        pos = self.positions.setdefault(market_id, PositionState(market_id, ""))
        meta = meta or {}
        if side == "BUY":
            if token_side == "UP":
                if pos.up_qty == 0:
                    pos.avg_up = price
                else:
                    pos.avg_up = (pos.avg_up * pos.up_qty + price * qty) / (pos.up_qty + qty)
                pos.up_qty += qty
            else:
                if pos.down_qty == 0:
                    pos.avg_down = price
                else:
                    pos.avg_down = (pos.avg_down * pos.down_qty + price * qty) / (pos.down_qty + qty)
                pos.down_qty += qty
            # 首笔建仓成交 → 计入 entry 次数（反马丁格尔铁律依据）
            if meta.get("module") == "entry" and meta.get("first_fill"):
                self.on_entry(market_id, token_side)
            if meta.get("module") == "tail_capture":
                pos.tail_held = True
                pos.tail_pending = False
                self._active_tail_market[pos.asset] = market_id
            if meta.get("module") == "xrp_fade":
                pos.fade_held = True
                pos.fade_pending = False
                self._active_fade_market[pos.asset] = market_id
            if meta.get("module") == "mid_capture":
                pos.mid_held = True
                pos.mid_pending = False
                pos.mid_exit = str(meta.get("exit_mode") or "hold")
                self._active_mid_market[pos.asset] = market_id
        else:  # SELL：减仓
            pos.exit_pending = False  # 出场成交（含部分成交后重挂）
            if token_side == "UP":
                pos.up_qty = max(Decimal("0"), pos.up_qty - qty)
                if pos.up_qty == 0:
                    pos.avg_up = None
            else:
                pos.down_qty = max(Decimal("0"), pos.down_qty - qty)
                if pos.down_qty == 0:
                    pos.avg_down = None
            # 尾部止盈出场清仓 → 释放按资产锁，允许下一窗口重新入场
            if pos.tail_held and pos.up_qty == 0 and pos.down_qty == 0:
                pos.tail_held = False
                pos.tail_pending = False
                if self._active_tail_market.get(pos.asset) == market_id:
                    del self._active_tail_market[pos.asset]
        # 实验成交记入各自独立风控实例，不进入主账本风控
        if meta.get("module") == "xrp_fade" and self.fade_risk is not None:
            self.fade_risk.on_fill(market_id, token_side, qty, price, side)
        elif meta.get("module") == "mid_capture" and self.mid_risk is not None:
            self.mid_risk.on_fill(market_id, token_side, qty, price, side)
        elif side == "SELL" and pos.mid_held and self.mid_risk is not None:
            # mid profit3 出场成交（订单 meta=exit）→ 仍记入实验账本
            self.mid_risk.on_fill(market_id, token_side, qty, price, side)
        else:
            self.risk.on_fill(market_id, token_side, qty, price, side)
        self._update_state(pos)

    def on_order_expired(self, market_id: int) -> None:
        """挂单过期/取消且无持仓 → 允许重新评估入场。"""
        pos = self.positions.get(market_id)
        if pos is None:
            return
        pos.tail_pending = False
        pos.exit_pending = False
        pos.fade_pending = False
        pos.mid_pending = False
        if pos.state == S1_SEEKING and pos.up_qty == 0 and pos.down_qty == 0:
            pos.state = S0_NO_POSITION

    def on_entry(self, market_id: int, direction: str) -> None:
        pos = self.positions.setdefault(market_id, PositionState(market_id, ""))
        if direction == "UP":
            pos.entries_up += 1
        else:
            pos.entries_down += 1
        self.risk.on_entry(market_id, direction)

    def on_settlement(self, market_id: int, result: str, pnl: Decimal) -> None:
        pos = self.positions.pop(market_id, None)
        if pos is not None:
            pos.state = S7_SETTLEMENT
            if pos.tail_held and self._active_tail_market.get(pos.asset) == market_id:
                del self._active_tail_market[pos.asset]
            if pos.fade_held and self._active_fade_market.get(pos.asset) == market_id:
                del self._active_fade_market[pos.asset]
            if pos.mid_held and self._active_mid_market.get(pos.asset) == market_id:
                del self._active_mid_market[pos.asset]
        # 实验窗口 PnL 记入各自独立风控，不污染主账本（连亏/日亏/回撤）
        if pos is not None and pos.fade_held and self.fade_risk is not None:
            self.fade_risk.on_settlement(market_id, pnl)
        elif pos is not None and pos.mid_held and self.mid_risk is not None:
            self.mid_risk.on_settlement(market_id, pnl)
        else:
            self.risk.on_settlement(market_id, pnl)

    def _update_state(self, pos: PositionState) -> None:
        if pos.up_qty > 0 and pos.down_qty > 0:
            pos.state = S5_COMPLETE_SET if abs(pos.up_qty - pos.down_qty) <= Decimal("0.001") else S4B_HEDGE
        elif pos.up_qty > 0 or pos.down_qty > 0:
            pos.state = S3_WAIT_REPRICING
        else:
            pos.state = S0_NO_POSITION

    # ── 工具 ─────────────────────────────────────────────────
    def _maybe_reset_periodic(self, ts_ms) -> None:
        """UTC 日/小时边界重置风控的日/小时累计 PnL（主账本与实验账本同步）。"""
        if not isinstance(ts_ms, int) or ts_ms <= 0:
            return
        day = ts_ms // 86_400_000
        hour = (ts_ms % 86_400_000) // 3_600_000
        if self._last_day is None:
            self._last_day, self._last_hour = day, hour
            return
        if day != self._last_day:
            self._last_day = day
            self.risk.reset_daily()
            for exp_risk in (self.fade_risk, self.mid_risk):
                if exp_risk is not None:
                    exp_risk.reset_daily()
        if hour != self._last_hour:
            self._last_hour = hour
            self.risk.reset_hourly()
            for exp_risk in (self.fade_risk, self.mid_risk):
                if exp_risk is not None:
                    exp_risk.reset_hourly()

    def _taker_fee(self, rec) -> float:
        """保守 taker 费率（小数）；实盘 Phase 5 按 feeSchedule 读取。"""
        return float(self.config.s("fees.taker_fee_bps", 1.0)) / 1e4
