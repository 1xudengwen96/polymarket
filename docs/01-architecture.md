# 01 — 总体架构（Architecture）

## 1. 技术选型

| 层 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.11+，asyncio 单进程事件循环 | 官方 `polymarket-client` SDK 原生 async；单进程足够 5m 节奏 |
| 市场/交易 SDK | `polymarket-client`（官方 py-sdk，含 Streams + SecureClient） | 旧 py-clob-client 已停用；新 SDK 同时封装 REST/WS/TWAP |
| 行情特征源 | RTDS `crypto_prices`（Binance 参考价）+ Binance 公开 WS aggTrades（逐笔，用于攻击量/订单流特征） | RTDS 是官方参考价；逐笔特征需 Binance 直接订阅 |
| 结算变量源 | RTDS `crypto_prices_twap_*`（Chainlink TWAP） | 与 oracle 完全同源 |
| 数据库 | 开发：SQLite（WAL）；生产：PostgreSQL 16 + TimescaleDB（Docker Compose） | 同构 SQL，hypertable 只影响建表 DDL |
| 配置/密钥 | `.env` + `pydantic-settings`，分层（base/paper/live） | 密钥绝不硬编码 |
| 进程编排 | 单进程 + 内部 task 组；Docker 部署 | 避免多进程状态同步复杂度 |
| 日志/监控 | structlog + JSON；`telemetry` 表 | 每决策可复盘 |

## 2. 进程内组件（全部为 asyncio 长驻 task，由 Supervisor 管理）

```
┌────────────────────────────── Bot Process (asyncio) ──────────────────────────────┐
│                                                                                    │
│  ┌────────────┐  ┌────────────┐  ┌─────────────┐  ┌──────────────┐                │
│  │ FeedManager │  │ MarketReg  │  │ TWAPCapture │  │ TickRecorder │  数据面        │
│  │  (WS聚合)   │  │(市场注册表)│  │  (PTB/结算) │  │ (BTC逐笔/特征)│                │
│  └─────┬──────┘  └─────┬──────┘  └──────┬──────┘  └──────┬───────┘                │
│        │               │                │                 │                        │
│        ▼               ▼                ▼                 ▼                        │
│  ┌──────────────────────────────────────────────────────────────────┐             │
│  │              Shared State (MarketSnapshot / FeatureStore)         │             │
│  │  每资产每窗口: book, trades, tick缓存, TWAP, 特征值, 时钟状态      │             │
│  └──────┬───────────────────────────────────────────────────────────┘             │
│         ▼                                                                          │
│  ┌───────────────┐   ┌──────────────┐   ┌───────────────┐   ┌─────────────────┐   │
│  │ StrategyEngine│   │ProbabilityEng│   │  RiskEngine   │   │ ExecutionEngine │   │
│  │  (状态机+4模块)│◄─►│ (特征→P校准) │   │ (限额/熔断)   │◄─►│ (订单管理/成交)  │   │
│  └───────┬───────┘   └──────────────┘   └───────┬───────┘   └────────┬────────┘   │
│          │                                      │                    │            │
│          ▼                                      ▼                    ▼            │
│  ┌─────────────────────────────────────────────────────────────────────────┐     │
│  │  Exchange Gateway (接口) ──► LiveAdapter ──► CLOB REST/WS + RTDS          │     │
│  │                          └─► PaperAdapter ──► PaperFillSim (模拟撮合)     │     │
│  └─────────────────────────────────────────────────────────────────────────┘     │
│         │                                                                          │
│         ▼                                                                          │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────┐                             │
│  │  Persistence│  │   Reporter   │  │  Web 控制台   │   (后续阶段)                 │
│  │ (SQLAlchemy)│  │ (指标/JSONL) │  │ (FastAPI只读) │                             │
│  └────────────┘  └──────────────┘  └───────────────┘                             │
└───────────────────────────────────────────────────────────────────────────┘
```

## 3. 组件职责

| 组件 | 职责 | 关键点 |
|---|---|---|
| **Supervisor** | 启动/重启/优雅停机；所有 task 的异常上报 | 崩溃→重连→状态对账（DB 是唯一真相） |
| **MarketReg** | 从 slug 推导当前/下一窗口；拉 Gamma 事件→token IDs→注册到 SharedState；轮询 `closed`/`outcomePrices` 对账 | 每个 5m 边界做窗口切换 |
| **FeedManager** | Market WS（book/price_change/last_trade/tick_change）+ User WS（trade/order）聚合 | `custom_feature_enabled=true`；按 hash 丢弃乱序 book 快照 |
| **TWAPCapture** | RTDS TWAP 流：窗口开始采样 PTB；持续追踪当前 TWAP；t_end 采样结算值 | 自结算优先于链上结算；断线告警 |
| **TickRecorder** | Binance aggTrades + RTDS crypto_prices：1s/3s/5s/10s/30s/60s 收益、波动率、aggressive buy/sell 量、Trade Flow | 环形缓存，O(1) 更新特征 |
| **ProbabilityEngine** | 特征向量 → Fair P(Up) → 校准 → 分桶统计 | 见 05 文档 |
| **StrategyEngine** | 状态机 + CheapEntry/Repricing/Hedge/CompleteSet/TailCapture/TailHedge/Arb 决策 | 见 04 文档 |
| **RiskEngine** | 全部限额检查（每决策前必过）；熔断；PnL/回撤监控 | 见 06 文档 |
| **ExecutionEngine** | 下单（GTC/GTD/post-only 优先）、撤单、替换、超时、幂等、限速、心跳 | 见 07 文档 |
| **Persistence** | 所有表写入；决策/订单/成交/结算全链路记录 | 见 03 文档 |
| **Reporter** | 指标聚合（win rate/expectancy/PF/Sharpe…）、日志导出 | 复盘口径与回测一致 |

## 4. 运行模式

```
LIVE=false  → Paper 模式（默认）：LiveAdapter 只读 + PaperAdapter 撮合，真实行情实时驱动
LIVE=true   → 实盘：需要 POLYMARKET_PRIVATE_KEY 等环境变量；极小资金
MODE=backtest → 历史数据离线重放（独立入口，同一策略/Risk/Execution 内核）
```
- 三个模式共用同一套 Strategy/Risk/Execution 内核，仅 `Exchange Gateway` 适配器不同（这是防止 paper 与 live 行为漂移的核心设计）。
- LIVE 开启需要：`LIVE=true` + 私钥存在 + 配置文件里 `allow_live_override` 的人工确认开关（默认关）。

## 5. 多资产扩展（Phase 2）
- 所有组件以 `asset` 为维度实例化：`btc` 现在；`eth`/`sol` 后续通过配置数组 `assets: [btc]` 扩展。
- 每个资产独立：窗口推导（slug）、TWAP 订阅符号（`btc/usd`/`eth/usd`/`sol/usd`）、Binance 符号（`btcusdt`…）、独立 Risk 限额。
- 共享：账户级 Exposure/PnL 限额在 RiskEngine 全局层。

## 6. 部署
```
docker-compose.yml:
  bot (python:3.12-slim, 本仓库)
  postgres:16 + timescale/timescaledb
  (可选) redis（跨窗口协调，阶段二）
.env 注入密钥；健康检查 /healthz（FastAPI 只读端点）
```
