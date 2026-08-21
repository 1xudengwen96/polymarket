"""ORM 模型（docs/03-database-schema.md）。

约定：
- 金额/价格/概率一律 TEXT（Decimal 字符串），禁用浮点记账；
- 时间序列列用 ts_ms BIGINT（unix 毫秒）；生产 Timescale 迁移时以其建 hypertable；
- 幂等主键：orders.client_order_id。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite 仅对 INTEGER PRIMARY KEY 自增；BigInteger 用 variant 降级（PG 上仍是 BIGINT）
AutoBigInt = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class RuntimeSetting(Base):
    """Dashboard-managed controls consumed by the running bot."""

    __tablename__ = "runtime_settings"

    setting_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(128))
    updated_ts_ms: Mapped[int] = mapped_column(BigInteger)


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    series_slug: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    asset: Mapped[str] = mapped_column(String(8), index=True)
    duration_s: Mapped[int] = mapped_column(Integer)
    t_start: Mapped[int] = mapped_column(BigInteger, index=True)
    t_end: Mapped[int] = mapped_column(BigInteger)
    condition_id: Mapped[str] = mapped_column(String(80))
    token_up: Mapped[str] = mapped_column(String(160))
    token_down: Mapped[str] = mapped_column(String(160))
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    twap_lookback_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tick_size: Mapped[str | None] = mapped_column(String(32), nullable=True)
    min_order_size: Mapped[str | None] = mapped_column(String(32), nullable=True)
    neg_risk: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fee_schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_markets_asset_tstart", "asset", "t_start", unique=True),)


class MarketStatus(Base):
    __tablename__ = "market_status"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    state: Mapped[str] = mapped_column(String(24))
    accepting_orders: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    gamma_closed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    gamma_outcome_prices: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class TwapSample(Base):
    __tablename__ = "twap_samples"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    window_s: Mapped[int] = mapped_column(Integer)
    value_e18: Mapped[str] = mapped_column(String(64))
    obs_ts_ms: Mapped[int] = mapped_column(BigInteger)
    src: Mapped[str] = mapped_column(String(16))


class Tick(Base):
    __tablename__ = "ticks"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    asset: Mapped[str] = mapped_column(String(8), index=True)
    price: Mapped[str] = mapped_column(String(40))
    vol_1s: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agg_buy_1s: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agg_sell_1s: Mapped[str | None] = mapped_column(String(40), nullable=True)
    n_trades_1s: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BookSnapshot(Base):
    __tablename__ = "book_snapshots"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    token_id: Mapped[str] = mapped_column(String(160), index=True)
    book_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    best_bid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    best_ask: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bid10: Mapped[str | None] = mapped_column(Text, nullable=True)
    ask10: Mapped[str | None] = mapped_column(Text, nullable=True)
    tick_size: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    market_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    token_id: Mapped[str] = mapped_column(String(160), index=True)
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[str] = mapped_column(String(32))
    size: Mapped[str] = mapped_column(String(40))
    taker_side: Mapped[str | None] = mapped_column(String(8), nullable=True)


class Settlement(Base):
    __tablename__ = "settlements"

    market_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ptb_e18: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_e18: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ptb_obs_ts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    final_obs_ts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    self_result: Mapped[str | None] = mapped_column(String(8), nullable=True)
    self_settled_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gamma_result: Mapped[str | None] = mapped_column(String(8), nullable=True)
    gamma_prices: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False)
    dispute: Mapped[str | None] = mapped_column(Text, nullable=True)
    ptb_src: Mapped[str | None] = mapped_column(String(16), nullable=True)


class DecisionLog(Base):
    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    market_id: Mapped[int] = mapped_column(BigInteger, index=True)
    asset: Mapped[str | None] = mapped_column(String(8), nullable=True)
    window_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ref_price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    btc_price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    twap_now: Mapped[str | None] = mapped_column(String(64), nullable=True)
    twap30_now: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remaining_s: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    into_window_s: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    up_bid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    up_ask: Mapped[str | None] = mapped_column(String(32), nullable=True)
    down_bid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    down_ask: Mapped[str | None] = mapped_column(String(32), nullable=True)
    spread: Mapped[str | None] = mapped_column(String(32), nullable=True)
    up_depth: Mapped[str | None] = mapped_column(Text, nullable=True)
    down_depth: Mapped[str | None] = mapped_column(Text, nullable=True)
    fair_prob: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cal_prob: Mapped[str | None] = mapped_column(String(32), nullable=True)
    market_prob: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gross_edge: Mapped[str | None] = mapped_column(String(32), nullable=True)
    net_edge: Mapped[str | None] = mapped_column(String(32), nullable=True)
    norm_distance: Mapped[str | None] = mapped_column(String(48), nullable=True)
    vol_10s: Mapped[str | None] = mapped_column(String(48), nullable=True)
    vol_60s: Mapped[str | None] = mapped_column(String(48), nullable=True)
    momentum: Mapped[str | None] = mapped_column(Text, nullable=True)
    obi: Mapped[str | None] = mapped_column(String(48), nullable=True)
    tfi: Mapped[str | None] = mapped_column(String(48), nullable=True)
    agg_buy: Mapped[str | None] = mapped_column(String(48), nullable=True)
    agg_sell: Mapped[str | None] = mapped_column(String(48), nullable=True)
    reversal_score: Mapped[str | None] = mapped_column(String(48), nullable=True)
    pos_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    pos_up_qty: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pos_down_qty: Mapped[str | None] = mapped_column(String(40), nullable=True)
    avg_entry_up: Mapped[str | None] = mapped_column(String(32), nullable=True)
    avg_entry_down: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hedge_cost: Mapped[str | None] = mapped_column(String(32), nullable=True)
    complete_set_cost: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision: Mapped[str] = mapped_column(String(24))
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fill_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fill_qty: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exit_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fees: Mapped[str | None] = mapped_column(String(40), nullable=True)
    slippage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    final_result: Mapped[str | None] = mapped_column(String(24), nullable=True)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)


class Order(Base):
    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_id: Mapped[int] = mapped_column(BigInteger, index=True)
    token_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tif: Mapped[str | None] = mapped_column(String(8), nullable=True)
    post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(16), index=True)
    clob_order_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    order_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    filled_qty: Mapped[str | None] = mapped_column(String(40), nullable=True)
    avg_fill_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_ts_ms: Mapped[int] = mapped_column(BigInteger)
    updated_ts_ms: Mapped[int] = mapped_column(BigInteger)
    expires_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    salt: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meta: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: strategy 元数据


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    market_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    price: Mapped[str] = mapped_column(String(32))
    qty: Mapped[str] = mapped_column(String(40))
    fee: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fee_bps: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger)
    src: Mapped[str] = mapped_column(String(16))


class Position(Base):
    __tablename__ = "positions"

    market_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    side_label: Mapped[str | None] = mapped_column(String(8), nullable=True)
    qty: Mapped[str | None] = mapped_column(String(40), nullable=True)
    avg_entry: Mapped[str | None] = mapped_column(String(32), nullable=True)
    realized_pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fees: Mapped[str | None] = mapped_column(String(40), nullable=True)
    state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    settled_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    complete_set_cost: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locked_profit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    roi: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capital_efficiency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    time_to_settlement_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opened_ts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    settled_ts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshot"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    mode: Mapped[str] = mapped_column(String(8))
    equity: Mapped[str] = mapped_column(String(40))
    available: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exposure_directional: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exposure_unhedged: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exposure_complete: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exposure_market: Mapped[str | None] = mapped_column(String(40), nullable=True)
    hourly_pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)
    daily_pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)
    drawdown: Mapped[str | None] = mapped_column(String(40), nullable=True)
    consecutive_losses: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CalibrationBucket(Base):
    __tablename__ = "calibration_buckets"

    bucket_low: Mapped[str] = mapped_column(String(16), primary_key=True)
    bucket_high: Mapped[str] = mapped_column(String(16), primary_key=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    actual_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    brier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ece_contrib: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_ts_ms: Mapped[int] = mapped_column(BigInteger)


class StrategyStatsDaily(Base):
    __tablename__ = "strategy_stats_daily"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    n_markets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fees: Mapped[str | None] = mapped_column(String(40), nullable=True)
    win_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    avg_win: Mapped[str | None] = mapped_column(String(40), nullable=True)
    avg_loss: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expectancy: Mapped[str | None] = mapped_column(String(40), nullable=True)
    profit_factor: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sharpe: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sortino: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_drawdown: Mapped[str | None] = mapped_column(String(40), nullable=True)
    turnover: Mapped[str | None] = mapped_column(String(40), nullable=True)
    avg_holding_s: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hedge_success_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    complete_set_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tail_capture_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    arb_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entry_edge_avg: Mapped[str | None] = mapped_column(String(32), nullable=True)


class PriceHistory(Base):
    """CLOB prices-history（回测市场概率序列）。"""

    __tablename__ = "pm_price_history"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    price: Mapped[str] = mapped_column(String(32))
    side: Mapped[str | None] = mapped_column(String(4), nullable=True)  # UP|DOWN


class MarketLabel(Base):
    """历史窗口的 gamma 结算标签（回测 ground truth）。"""

    __tablename__ = "market_labels"

    market_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    asset: Mapped[str] = mapped_column(String(8), index=True)
    t_start: Mapped[int] = mapped_column(BigInteger, index=True)
    gamma_result: Mapped[str | None] = mapped_column(String(8), nullable=True)  # UP|DOWN
    gamma_prices: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gamma_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    # TWAP 自算标签（对账用）
    twap_ptb: Mapped[str | None] = mapped_column(String(64), nullable=True)
    twap_final: Mapped[str | None] = mapped_column(String(64), nullable=True)
    twap_result: Mapped[str | None] = mapped_column(String(8), nullable=True)
    twap_margin_bps: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mismatch: Mapped[bool] = mapped_column(Boolean, default=False)


class FeatureRow(Base):
    """回测特征数据集（模型训练）。"""

    __tablename__ = "features_dataset"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(BigInteger, index=True)
    asset: Mapped[str] = mapped_column(String(8), index=True)
    t_start: Mapped[int] = mapped_column(BigInteger, index=True)
    sample_offset_s: Mapped[int] = mapped_column(Integer)  # 窗口内采样时刻
    label: Mapped[int] = mapped_column(Integer)  # 1=UP赢 0=DOWN赢
    features: Mapped[str] = mapped_column(Text)  # JSON
    split: Mapped[str] = mapped_column(String(8))  # train|val|test
