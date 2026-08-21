# Polymarket Crypto 5m Up/Down HFT Bot (pm5hft)

概率重定价 + 动态库存滚动 + 对冲套利机器人。Phase 1 仅 BTC 5 分钟 Up/Down 市场。
默认 **Paper Trading**；实盘必须显式双开关（`PM5HFT_LIVE=true` + `config/live.yaml` 的 `allow_live: true`）。

设计文档见 [`docs/`](docs/)，API 验证报告见 [`docs/00-api-verification.md`](docs/00-api-verification.md)。

## 快速开始（Paper）

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
copy .env.example .env          # 或设置环境变量
python -m pm5hft.main           # 默认 paper 模式
```

## 运行模式

| 模式 | 命令 | 说明 |
|---|---|---|
| Paper（默认） | `python -m pm5hft.main` | 真实行情实时驱动，本地模拟撮合 |
| Live | `PM5HFT_LIVE=true python -m pm5hft.main --mode live` | 需私钥 + `allow_live:true` |
| Backtest | `python -m pm5hft.backtest run`（Phase 3） | 历史回放 |

## 交易日程（止盈目标 / 北京时间交易时段）

机器人支持两种“休息”设置，达到条件后只管理已有持仓、不再开新仓（已挂的入场单自动撤销），
条件解除后自动恢复交易。可在监控面板（Dashboard）顶部控件直接设置，默认值在
`config/strategy.yaml` 的 `schedule:` 段：

| 设置 | 说明 | 默认 |
|---|---|---|
| 每日止盈目标（USDT） | 当日已实现利润 ≥ 该值 → 当日休息（0 = 关闭）。计数随机器人启动清零，北京时间 0 点重置 | `0`（关闭） |
| 进场价（尾部策略） | 尾盘策略**最高可接受买价**（¢）。挂单价不高于它：默认 `98`；设 `80` = 只在市场价 ≥80¢ 时参与、且成交价不超过 80¢（市场价高于 80 就不成交）。进场价 <95¢ 时自动启用模型闸门（校准概率 ≥ 市价+buffer 才买），防低价位负 EV | `98`¢ |
| 出场价（尾部策略） | 尾部止盈出场价（¢）。`0` = 关闭（持有到结算，默认）；`>0` = 持仓方 bid 涨到该价就 FAK 卖出落袋（例：进场 90 / 出场 99）。出场价须高于进场价 | `0`（关闭） |
| 交易时段（北京时间） | 开启后仅在 `开始 — 结束` 小时之间交易；支持跨夜（如 22→6 = 22:00–05:59） | 关闭（全天） |

休息原因（`manual` 手动关闭 / `profit_target` 止盈达成 / `trading_hours` 时段外）会显示在面板
“休息状态”上，并写入 `runtime_settings.rest_reason` 供外部查询。

## 目录

```
pm5hft/
  main.py         入口
  supervisor.py   任务编排/看门狗
  config.py       配置加载（env 覆盖 yaml）
  clock.py        窗口时钟/slug 推导
  market_registry.py  Gamma 市场注册
  twap.py         PTB 捕获 + 自结算
  feeds/          CLOB Market WS / RTDS / Binance
  features/       特征与环形缓存
  persistence/    SQLAlchemy 仓储
  models.py       全部 ORM 表
config/           策略/风险/执行/概率/资产 yaml
```

## 安全铁律（代码级强制）

- 禁止 Martingale / 无限补仓 / 亏钱对冲（`docs/04-strategy-engine.md` §11）
- 所有敏感信息走环境变量
- 回测达标前禁止实盘（`docs/08-backtest-engine.md` §5）
