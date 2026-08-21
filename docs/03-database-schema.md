# 03 — 数据库 Schema

SQLite（开发/单机）与 PostgreSQL+TimescaleDB（生产）同构；`time` 列在 PG 上为 hypertable 维度。
所有金额用整数分（cents）或 Decimal 字符串存储，**禁用浮点记账**。

## 1. markets — 市场/窗口元数据

```sql
CREATE TABLE markets (
  id            BIGINT PRIMARY KEY,          -- gamma market id
  event_id      BIGINT NOT NULL,
  series_slug   TEXT NOT NULL,               -- 'btc-up-or-down-5m'
  slug          TEXT NOT NULL UNIQUE,        -- 'btc-updown-5m-1786677000'
  asset         TEXT NOT NULL,               -- btc | eth | sol
  duration_s    INTEGER NOT NULL,            -- 300
  t_start       BIGINT NOT NULL,             -- unix 秒（权威窗口起点）
  t_end         BIGINT NOT NULL,
  condition_id  TEXT NOT NULL,
  token_up      TEXT NOT NULL,               -- clobTokenIds[0]
  token_down    TEXT NOT NULL,
  question      TEXT,
  resolution_source TEXT,
  twap_lookback_s INTEGER,                   -- cryptoMarketConfig.twapLookbackSeconds（60）
  tick_size     TEXT,                        -- '0.01'
  min_order_size TEXT,                       -- '5'
  neg_risk      BOOLEAN,
  fee_schedule  TEXT,                        -- JSON 原文
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE(asset, t_start)
);

CREATE TABLE market_status (                 -- 状态变迁历史
  id            BIGSERIAL PRIMARY KEY,
  market_id     BIGINT NOT NULL REFERENCES markets(id),
  ts            BIGINT NOT NULL,
  state         TEXT NOT NULL,               -- discovered|trading|closing|settled|reconciled
  accepting_orders BOOLEAN,
  gamma_closed  BOOLEAN,
  gamma_outcome_prices TEXT,                 -- '["0","1"]'
  detail        TEXT
);
```

## 2. settlement — TWAP 采样与结算

```sql
CREATE TABLE twap_samples (
  time          TIMESTAMPTZ NOT NULL,
  symbol        TEXT NOT NULL,               -- 'btc/usd'
  window_s      INTEGER NOT NULL,            -- 30 | 60
  value_e18     TEXT NOT NULL,               -- full_accuracy_value（定点原文）
  obs_ts        BIGINT NOT NULL,             -- Chainlink 观察时间(ms)
  src           TEXT NOT NULL                -- rtds | binance_approx
);

CREATE TABLE settlements (
  market_id     BIGINT PRIMARY KEY REFERENCES markets(id),
  ptb_e18       TEXT,                        -- 窗口开始 TWAP
  final_e18     TEXT,                        -- 窗口结束 TWAP
  ptb_obs_ts    BIGINT, final_obs_ts BIGINT,
  self_result   TEXT,                        -- UP | DOWN | UNKNOWN
  self_settled_at BIGINT,
  gamma_result  TEXT,                        -- UP | DOWN（对账后）
  gamma_prices  TEXT,
  reconciled    BOOLEAN DEFAULT FALSE,
  dispute       TEXT,                        -- 对账不一致说明
  ptb_src       TEXT                         -- rtds | approx | missing
);
```

## 3. ticks / books / trades — 行情持久化（供复盘与特征重建）

```sql
CREATE TABLE ticks (                          -- BTC 1s 聚合 + 逐笔特征
  time     TIMESTAMPTZ NOT NULL,
  asset    TEXT NOT NULL,
  price    TEXT, vol_1s TEXT,
  agg_buy_1s TEXT, agg_sell_1s TEXT, n_trades_1s INTEGER
);

CREATE TABLE book_snapshots (                 -- 事件级快照（按需稀疏落库）
  time       TIMESTAMPTZ NOT NULL,
  token_id   TEXT NOT NULL,
  book_hash  TEXT,
  best_bid TEXT, best_ask TEXT,
  bid10 TEXT, ask10 TEXT,                     -- 前十档 JSON（执行/复盘用）
  tick_size TEXT
);

CREATE TABLE trades (
  time     TIMESTAMPTZ NOT NULL,
  market_id BIGINT, token_id TEXT,
  side     TEXT, price TEXT, size TEXT,       -- CLOB 公开成交
  taker_side TEXT                            -- BUY|SELL 攻击方向（可由成交相对中间价推断）
);
```

## 4. decision_log — 每一次决策完整记录（用户要求 §17 全字段）

```sql
CREATE TABLE decision_log (
  id             BIGSERIAL PRIMARY KEY,
  ts             BIGINT NOT NULL,             -- 决策时间(ms)
  market_id      BIGINT NOT NULL,
  asset          TEXT, window_idx INTEGER,    -- 窗口序号
  ref_price      TEXT,                        -- PTB
  btc_price      TEXT, twap_now TEXT, twap30_now TEXT,
  remaining_s    INTEGER, into_window_s INTEGER,
  up_bid TEXT, up_ask TEXT, down_bid TEXT, down_ask TEXT,
  spread TEXT, up_depth TEXT, down_depth TEXT,   -- 可成交深度摘要 JSON
  fair_prob      TEXT, cal_prob TEXT, market_prob TEXT,
  gross_edge     TEXT, net_edge TEXT,
  norm_distance  TEXT,                        -- (twap_now-PTB)/PTB 或分位数
  vol_10s TEXT, vol_60s TEXT,
  momentum       TEXT,                        -- 短周期收益向量 JSON
  obi            TEXT,                        -- order book imbalance
  tfi            TEXT,                        -- trade flow imbalance
  agg_buy TEXT, agg_sell TEXT,
  reversal_score TEXT,
  pos_state      TEXT,                        -- 状态机 STATE_0..7 当前值
  pos_up_qty     TEXT, pos_down_qty TEXT,     -- 本窗口持仓
  avg_entry_up   TEXT, avg_entry_down TEXT,
  hedge_cost     TEXT, complete_set_cost TEXT,
  decision       TEXT,                        -- NOOP|ENTER_UP|ENTER_DOWN|HEDGE_DOWN|HEDGE_UP|
                                              -- EXIT_PROFIT|EXIT_STOP|TAIL_CAPTURE|TAIL_HEDGE|ARB|CANCEL
  reject_reason  TEXT,                        -- 被风险/策略拒绝原因（复盘必填）
  client_order_id TEXT,
  order_id       TEXT, fill_price TEXT, fill_qty TEXT,
  exit_price     TEXT, pnl TEXT, fees TEXT, slippage TEXT,
  final_result   TEXT,                        -- 窗口结算后回填
  extra          TEXT                         -- JSON：完整特征向量
);
CREATE INDEX idx_decision_ts ON decision_log(ts);
```

## 5. orders / fills — 订单与成交（幂等核心）

```sql
CREATE TABLE orders (
  client_order_id TEXT PRIMARY KEY,           -- 我们生成的幂等键（uuid7）
  market_id       BIGINT NOT NULL,
  token_id        TEXT, side TEXT,
  price           TEXT, size TEXT,
  tif             TEXT,                       -- GTC|GTD|FOK|FAK
  post_only       BOOLEAN,
  mode            TEXT,                       -- paper | live
  state           TEXT,                       -- PENDING|LIVE|PARTIAL|FILLED|CANCELLED|EXPIRED|REJECTED
  clob_order_id   TEXT,                       -- 实盘 orderID
  order_hash      TEXT,                       -- 幂等对账哈希
  filled_qty      TEXT, avg_fill_price TEXT,
  reason          TEXT,                       -- 撤单/拒绝原因
  created_ts      BIGINT, updated_ts BIGINT,
  salt            TEXT                        -- 确定性 salt 记录
);
CREATE INDEX idx_orders_market ON orders(market_id);

CREATE TABLE fills (
  id          BIGSERIAL PRIMARY KEY,
  order_id    TEXT REFERENCES orders(client_order_id),
  market_id   BIGINT, token_id TEXT, side TEXT,
  price       TEXT, qty TEXT,
  fee         TEXT, fee_bps TEXT,
  ts          BIGINT,
  src         TEXT                            -- clob|user_ws|paper_sim|reconcile
);
```

## 6. positions / capital — 仓位与资金

```sql
CREATE TABLE positions (                       -- 每窗口每 token 一行（滚动视图）
  market_id  BIGINT NOT NULL,
  token_id   TEXT NOT NULL,
  side_label TEXT,                             -- Up|Down
  qty        TEXT, avg_entry TEXT,
  realized_pnl TEXT, fees TEXT,
  state      TEXT,                             -- OPEN|HEDGED|LOCKED|SETTLED|ABANDONED
  settled_result TEXT,                         -- WIN|LOSS|COMPLETE
  complete_set_cost TEXT, locked_profit TEXT,
  roi TEXT, capital_efficiency TEXT, time_to_settlement_s INTEGER,
  opened_ts BIGINT, settled_ts BIGINT,
  PRIMARY KEY (market_id, token_id)
);

CREATE TABLE equity_snapshot (                 -- 周期快照（每小时/日 PnL、回撤）
  ts      TIMESTAMPTZ NOT NULL,
  mode    TEXT,                                -- paper|live
  equity  TEXT, available TEXT,
  exposure_directional TEXT, exposure_unhedged TEXT,
  exposure_complete TEXT, exposure_market TEXT,
  hourly_pnl TEXT, daily_pnl TEXT, drawdown TEXT,
  consecutive_losses INTEGER
);
```

## 7. calibration / stats — 模型与统计

```sql
CREATE TABLE calibration_buckets (             -- §14 概率校准分桶
  bucket_low  NUMERIC, bucket_high NUMERIC,    -- 0.50-0.55, ..., 0.99-1.00
  n           INTEGER, wins INTEGER,
  actual_rate NUMERIC, brier NUMERIC, ece_contrib NUMERIC,
  updated_ts  BIGINT,
  PRIMARY KEY(bucket_low, bucket_high)
);

CREATE TABLE strategy_stats_daily (
  day DATE PRIMARY KEY,
  mode TEXT,
  n_markets INTEGER, n_trades INTEGER,
  wins INTEGER, losses INTEGER,
  total_pnl TEXT, fees TEXT,
  win_rate TEXT, avg_win TEXT, avg_loss TEXT, expectancy TEXT,
  profit_factor TEXT, sharpe TEXT, sortino TEXT,
  max_drawdown TEXT, turnover TEXT, avg_holding_s TEXT,
  hedge_success_rate TEXT, complete_set_rate TEXT,
  tail_capture_rate TEXT, arb_rate TEXT,
  entry_edge_avg TEXT
);
```

## 8. 迁移与演进
- SQLAlchemy 2.0 + Alembic 迁移（开发初期可用 create_all + 版本标记）。
- Timescale：`ticks`/`book_snapshots`/`trades`/`twap_samples` 建 hypertable(chunk 1 day)。
- 复盘导出：`reporter export --day` 生成 CSV/JSONL（与回测输出同一 schema，便于对比）。
