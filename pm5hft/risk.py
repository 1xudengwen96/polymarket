"""风险引擎（docs/06）。

- 每笔决策前强制预检；拒绝写 reject_reason；
- 账户级限额 + 窗口级限额；单向棘轮熔断；
- 铁律（不可配置）：禁 Martingale / 禁亏钱对冲 / 禁跨窗口滚动 / PTB 缺失禁开仓。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .clock import now_ms
from .config import Config
from .logging_setup import get_logger


class RiskState(Enum):
    NORMAL = "NORMAL"
    COOLDOWN = "COOLDOWN"      # 禁开仓，允许退出/撤单
    KILL = "KILL"              # 全停，人工解锁


@dataclass
class WindowExposure:
    market_id: int
    up_qty: Decimal = Decimal("0")
    down_qty: Decimal = Decimal("0")
    up_notional: Decimal = Decimal("0")
    down_notional: Decimal = Decimal("0")
    entries_up: int = 0
    entries_down: int = 0


@dataclass
class PreTradeContext:
    market_id: int
    asset: str
    kind: str          # entry | hedge | exit | tail_capture | tail_hedge | arb
    side: str          # BUY | SELL
    token_side: str    # UP | DOWN
    qty: Decimal
    price: Decimal     # 预期成交价
    taker_fee: float   # 小数（如 0.0001）
    complete_set_cost: Decimal | None = None  # hedge 用：已有均价 + 对边价
    remaining_s: float = 300.0
    ptb_ready: bool = True
    entry_direction: str | None = None  # entry 用：UP|DOWN（同向加仓检测）
    # Dashboard 控制的固定金额入场由前端直接决定，不受账户敞口预算裁决。
    # 仍保留硬性交易守卫（PTB、截止时间、重复入场、交易所规则）。
    bypass_account_limits: bool = False


@dataclass
class RiskCheck:
    ok: bool
    reason: str | None = None
    blocked_code: str | None = None


class RiskEngine:
    def __init__(self, config: Config, mode: str = "paper", overrides: dict | None = None) -> None:
        self.config = config
        self.mode = mode
        self.log = get_logger("risk")
        r = config.risk
        ov = overrides or {}
        # live 模式：加载小资金风控档（config/live_risk.yaml）覆盖限额
        if mode == "live":
            live_ov = config.live_risk()
            fields = type(r).model_fields
            r = r.model_copy(update={k: v for k, v in live_ov.items() if k in fields})
            self.log.info("live risk profile applied", overrides=live_ov)
        self._risk_cfg = r  # 覆盖后的限额基准（所有限额换算必须读它）
        self.equity: Decimal = Decimal(str(r.paper_starting_equity))
        self.peak_equity: Decimal = self.equity
        self.daily_pnl: Decimal = Decimal("0")
        self.hourly_pnl: Decimal = Decimal("0")
        self.consecutive_losses: int = 0
        self.state: RiskState = RiskState.NORMAL
        self.windows: dict[int, WindowExposure] = {}
        self.cooloff_until_ms: int | None = None
        self.kill_reason: str | None = None
        # 亏损限额随资产数按 √n 缩放（多资产组合波动率原则性缩放；回撤 kill switch 不缩放）
        import math

        self._asset_scale = math.sqrt(max(1, len(config.enabled_assets())))
        self._daily_loss_pct = Decimal(str(ov.get("max_daily_loss_pct", r.max_daily_loss_pct)))
        self._hourly_loss_pct = Decimal(str(ov.get("max_hourly_loss_pct", r.max_hourly_loss_pct)))
        self._max_consecutive = int(ov.get("max_consecutive_losses", r.max_consecutive_losses))
        self._max_drawdown_pct = Decimal(str(ov.get("max_drawdown_pct", r.max_drawdown_pct)))
        self._daily_loss_abs = self._daily_loss_pct * self.equity * Decimal(str(self._asset_scale))
        self._hourly_loss_abs = self._hourly_loss_pct * self.equity * Decimal(str(self._asset_scale))

    # ── 限额换算 ─────────────────────────────────────────────
    def _max_initial(self) -> Decimal:
        return Decimal(str(self._risk_cfg.max_initial_exposure))

    def _max_unhedged(self) -> Decimal:
        # 组合级未对冲上限：随启用资产数扩展（每资产一个尾部仓）
        n_assets = max(1, len(self.config.enabled_assets()))
        per_asset = Decimal(str(self._risk_cfg.unhedged_exposure_per_asset_pct))
        base = Decimal(str(self._risk_cfg.max_unhedged_exposure_pct))
        pct = max(base, per_asset * n_assets)
        return pct * self.equity

    def _max_market(self) -> Decimal:
        return Decimal(str(self._risk_cfg.max_market_exposure_pct)) * self.equity

    def _max_window(self) -> Decimal:
        return Decimal(str(self._risk_cfg.max_window_notional))

    def window(self, market_id: int) -> WindowExposure:
        w = self.windows.get(market_id)
        if w is None:
            w = WindowExposure(market_id=market_id)
            self.windows[market_id] = w
        return w

    # ── 预检 ─────────────────────────────────────────────────
    def pre_trade(self, ctx: PreTradeContext) -> RiskCheck:
        if self.state == RiskState.KILL:
            return RiskCheck(False, f"kill switch active: {self.kill_reason}", "KILL_SWITCH")
        if self.state == RiskState.COOLDOWN:
            # 冷却期按窗口数计时（cooloff_windows × 300s），到期自动恢复
            if self.cooloff_until_ms is not None and now_ms() <= self.cooloff_until_ms:
                return RiskCheck(False, "cooldown active", "COOLDOWN")
            self.state = RiskState.NORMAL
            self.log.info("cooldown expired, risk state restored to NORMAL")

        notional = ctx.qty * ctx.price

        # 铁律 1：禁 Martingale（同窗口同方向加仓）
        w = self.window(ctx.market_id)
        if ctx.kind == "entry" and ctx.entry_direction == "UP" and w.entries_up >= 1:
            return RiskCheck(False, "same-side re-entry blocked (anti-martingale)", "MARTINGALE_BLOCK")
        if ctx.kind == "entry" and ctx.entry_direction == "DOWN" and w.entries_down >= 1:
            return RiskCheck(False, "same-side re-entry blocked (anti-martingale)", "MARTINGALE_BLOCK")

        # 铁律 2：禁亏钱对冲
        if ctx.kind == "hedge":
            if ctx.complete_set_cost is None:
                return RiskCheck(False, "hedge without complete set cost", "HEDGE_COST_MISSING")
            total = ctx.complete_set_cost + Decimal(str(2 * ctx.taker_fee))
            if total >= Decimal("1"):
                return RiskCheck(False, f"hedge would lock a loss (cost={total})", "HEDGE_LOSS_BLOCK")

        # 铁律 5：PTB 缺失禁开仓
        if ctx.kind in ("entry", "tail_capture", "arb") and not ctx.ptb_ready:
            return RiskCheck(False, "PTB missing", "PTB_MISSING_BLOCK")

        # 时间守卫（执行层 deadline 由策略处理，这里做硬下限）
        if ctx.kind in ("entry", "tail_capture", "arb") and ctx.remaining_s < 10:
            return RiskCheck(False, "hard deadline passed", "TIME_BLOCK")

        # 持仓时间：窗口结束必须结算（由结算流程强制，这里仅记录）

        # Dashboard 固定金额入场：金额直通，不用账户预算把订单拒掉。
        # 这只跳过可配置的账户/窗口限额，硬性交易守卫仍在上面执行。
        if ctx.bypass_account_limits:
            return RiskCheck(True)

        # 初始仓位上限
        if ctx.kind == "entry" and notional > self._max_initial():
            return RiskCheck(False, f"initial notional {notional} > max {self._max_initial()}", "MAX_INITIAL_EXPOSURE")

        # 未对冲敞口（组合级汇总，跨资产并发受此约束）
        if ctx.kind in ("entry", "tail_capture"):
            current_unhedged = sum(abs(x.up_notional - x.down_notional)
                                   for x in self.windows.values())
            add = notional if ctx.kind in ("entry", "tail_capture") else Decimal("0")
            if current_unhedged + add > self._max_unhedged():
                return RiskCheck(False, "unhedged exposure limit", "MAX_UNHEDGED_EXPOSURE")

        # 窗口名义
        if w.up_notional + w.down_notional + notional > self._max_window():
            return RiskCheck(False, "window notional limit", "MAX_WINDOW_NOTIONAL")

        # 全市场占用
        total_market = sum(x.up_notional + x.down_notional for x in self.windows.values())
        if total_market + notional > self._max_market():
            return RiskCheck(False, "market exposure limit", "MAX_MARKET_EXPOSURE")

        # 亏损限额
        if self.daily_pnl + self._daily_loss_abs < 0:
            return RiskCheck(False, "daily loss limit", "MAX_DAILY_LOSS")
        if self.hourly_pnl + self._hourly_loss_abs < 0:
            return RiskCheck(False, "hourly loss limit", "MAX_HOURLY_LOSS")
        if self.consecutive_losses >= self._max_consecutive:
            return RiskCheck(False, "consecutive losses limit", "MAX_CONSECUTIVE_LOSSES")

        return RiskCheck(True)

    # ── 成交/结算记账 ────────────────────────────────────────
    def on_fill(self, market_id: int, token_side: str, qty: Decimal, price: Decimal, side: str = "BUY") -> None:
        w = self.window(market_id)
        notional = qty * price
        if side == "BUY":
            if token_side == "UP":
                w.up_qty += qty
                w.up_notional += notional
            else:
                w.down_qty += qty
                w.down_notional += notional
        else:  # SELL：减仓
            if token_side == "UP":
                sold = min(qty, w.up_qty)
                frac = sold / w.up_qty if w.up_qty > 0 else Decimal("0")
                w.up_qty -= sold
                w.up_notional -= w.up_notional * frac
            else:
                sold = min(qty, w.down_qty)
                frac = sold / w.down_qty if w.down_qty > 0 else Decimal("0")
                w.down_qty -= sold
                w.down_notional -= w.down_notional * frac

    def on_entry(self, market_id: int, direction: str) -> None:
        w = self.window(market_id)
        if direction == "UP":
            w.entries_up += 1
        else:
            w.entries_down += 1

    def on_settlement(self, market_id: int, pnl: Decimal) -> None:
        """窗口结算：更新账户级状态；pnl 为该窗口已实现净盈亏（含费用）。"""
        self.windows.pop(market_id, None)
        self.equity += pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        self.daily_pnl += pnl
        self.hourly_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self._max_consecutive:
                self._cooldown()
        else:
            self.consecutive_losses = 0
        drawdown = (self.peak_equity - self.equity) / self.peak_equity
        if drawdown >= self._max_drawdown_pct:
            self.kill(f"max drawdown {drawdown:.2%}")

    def _cooldown(self) -> None:
        if self.state == RiskState.NORMAL:
            self.state = RiskState.COOLDOWN
            windows = int(self._risk_cfg.cooloff_windows or 12)
            self.cooloff_until_ms = now_ms() + windows * 300_000
            self.log.warning("cooldown triggered", consecutive_losses=self.consecutive_losses,
                             until_ms=self.cooloff_until_ms)

    def kill(self, reason: str) -> None:
        self.state = RiskState.KILL
        self.kill_reason = reason
        self.log.error("KILL SWITCH", reason=reason)

    def reset_daily(self) -> None:
        self.daily_pnl = Decimal("0")

    def reset_hourly(self) -> None:
        self.hourly_pnl = Decimal("0")

    def set_equity(self, value: Decimal) -> None:
        """实盘启动：用链上余额覆盖权益并重算亏损限额（限额基数必须与真实资金一致）。"""
        self.equity = value
        self.peak_equity = value
        self._daily_loss_abs = self._daily_loss_pct * value * Decimal(str(self._asset_scale))
        self._hourly_loss_abs = self._hourly_loss_pct * value * Decimal(str(self._asset_scale))
        self.log.info("risk equity set", equity=str(value),
                      daily_abs=str(self._daily_loss_abs), hourly_abs=str(self._hourly_loss_abs))

    def drawdown(self) -> Decimal:
        """当前回撤（相对峰值权益）。"""
        if self.peak_equity <= 0:
            return Decimal("0")
        return (self.peak_equity - self.equity) / self.peak_equity

    def snapshot(self) -> dict:
        return {
            "equity": str(self.equity),
            "daily_pnl": str(self.daily_pnl),
            "hourly_pnl": str(self.hourly_pnl),
            "consecutive_losses": self.consecutive_losses,
            "state": self.state.value,
            "exposure_unhedged": str(sum(abs(w.up_notional - w.down_notional) for w in self.windows.values())),
            "exposure_market": str(sum(w.up_notional + w.down_notional for w in self.windows.values())),
        }
