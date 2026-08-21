"""风险引擎铁律测试。"""

from decimal import Decimal

import pytest

from pm5hft.clock import now_ms
from pm5hft.config import Config
from pm5hft.risk import PreTradeContext, RiskEngine


@pytest.fixture()
def risk():
    cfg = Config()
    # 测试按 1 万本金口径断言（生产纸面配置为 1000，避免阈值路径变化）
    cfg.risk.paper_starting_equity = 10000.0
    return RiskEngine(cfg, mode="paper")


def ctx(**kw) -> PreTradeContext:
    base = dict(
        market_id=1, asset="btc", kind="entry", side="BUY", token_side="UP",
        qty=Decimal("50"), price=Decimal("0.43"), taker_fee=0.0001,
        remaining_s=200.0, ptb_ready=True, entry_direction="UP",
    )
    base.update(kw)
    return PreTradeContext(**base)


def test_entry_ok(risk):
    c = risk.pre_trade(ctx())
    assert c.ok


def test_martingale_blocked(risk):
    risk.on_entry(1, "UP")
    c = risk.pre_trade(ctx())
    assert not c.ok
    assert c.blocked_code == "MARTINGALE_BLOCK"


def test_hedge_loss_blocked(risk):
    # 已有 Up 成本 0.43，对边 0.60 → complete set cost 1.03 ≥ 1 → 拒绝
    c = risk.pre_trade(ctx(kind="hedge", token_side="DOWN", qty=Decimal("100"),
                            price=Decimal("0.60"), complete_set_cost=Decimal("1.03")))
    assert not c.ok
    assert c.blocked_code == "HEDGE_LOSS_BLOCK"


def test_hedge_profitable_ok(risk):
    c = risk.pre_trade(ctx(kind="hedge", token_side="DOWN", qty=Decimal("100"),
                            price=Decimal("0.47"), complete_set_cost=Decimal("0.90")))
    assert c.ok


def test_ptb_missing_blocks_entry(risk):
    c = risk.pre_trade(ctx(ptb_ready=False))
    assert not c.ok
    assert c.blocked_code == "PTB_MISSING_BLOCK"


def test_max_initial_exposure(risk):
    c = risk.pre_trade(ctx(qty=Decimal("1000"), price=Decimal("0.5")))  # 500 USDC
    assert not c.ok
    assert c.blocked_code == "MAX_INITIAL_EXPOSURE"


def test_dashboard_fixed_amount_bypasses_account_limits(risk):
    c = risk.pre_trade(ctx(
        kind="tail_capture", qty=Decimal("1000"), price=Decimal("0.98"),
        bypass_account_limits=True,
    ))
    assert c.ok


def test_daily_loss_and_drawdown(risk):
    # 7 资产下日亏线 = 1% × √7 × 10000 ≈ 264.6；-280 触发日亏且 <3% 回撤（不触发 kill）
    risk.on_settlement(1, Decimal("-280"))
    c = risk.pre_trade(ctx(market_id=2))
    assert not c.ok
    assert c.blocked_code == "MAX_DAILY_LOSS"


def test_kill_switch(risk):
    risk.kill("test")
    c = risk.pre_trade(ctx())
    assert not c.ok
    assert c.blocked_code == "KILL_SWITCH"


def test_consecutive_losses_cooldown(risk):
    for i in range(6):
        risk.on_settlement(1000 + i, Decimal("-1"))
    assert risk.state.value == "COOLDOWN"
    c = risk.pre_trade(ctx(market_id=99))
    assert not c.ok
    assert c.blocked_code == "COOLDOWN"


def test_cooldown_auto_expiry(risk):
    risk._cooldown()
    assert risk.state.value == "COOLDOWN"
    c = risk.pre_trade(ctx(market_id=99))
    assert not c.ok
    # 冷却期（12 窗口 × 300s）到期 → 自动恢复 NORMAL，允许新单
    risk.cooloff_until_ms = now_ms() - 1
    c2 = risk.pre_trade(ctx(market_id=99))
    assert c2.ok
    assert risk.state.value == "NORMAL"


def test_live_risk_profile_applied():
    import math

    cfg = Config()
    risk = RiskEngine(cfg, mode="live")
    # live_risk.yaml：日亏 12% / 小时 6% / 回撤 20% / 市场 20% / 每资产未对冲 6%
    assert risk._max_drawdown_pct == Decimal("0.20")
    # equity=1000（paper_starting_equity），√n=√2
    assert risk._daily_loss_abs == Decimal("0.12") * Decimal("1000") * Decimal(str(math.sqrt(2)))
    # 未对冲 = max(2%, 6%×2) = 12%
    assert risk._max_unhedged() == Decimal("0.12") * Decimal("1000")
    assert risk._max_market() == Decimal("0.20") * Decimal("1000")


def test_set_equity_recomputes_limits():
    cfg = Config()
    risk = RiskEngine(cfg, mode="live")
    risk.set_equity(Decimal("92"))
    assert risk.equity == Decimal("92")
    assert risk.peak_equity == Decimal("92")
    assert risk._max_market() == Decimal("0.20") * Decimal("92")
    assert risk._max_unhedged() == Decimal("0.12") * Decimal("92")
    # 单笔 5 USDC 在 92 权益下通过预检（关键：小资金可交易）
    c = risk.pre_trade(ctx(qty=Decimal("5"), price=Decimal("0.98")))
    assert c.ok, c.reason
