# 09 — Paper Trading Engine（模拟盘引擎）

默认模式（`LIVE=false`）。**用真实行情实时驱动，唯一不同是成交侧为本地模拟撮合。**

## 1. 定位
- Paper 是 live 的预演：同一进程、同一 Strategy/Risk/Execution 内核、同一 DB 表（`mode='paper'`）。
- 目标：完成 ≥ **1000 笔** paper trades，指标达到 08 §5 门槛，才允许人工确认后开启 LIVE。

## 2. PaperFillSim（模拟撮合器）

以 CLOB Market WS 的真实簿口事件为输入，维护本地模拟簿：

```
输入: 真实 book 快照 + price_change 增量（hash 校验，乱序丢弃）
状态: 每 token: 价位→{总量, 我方队列位置}; 我方挂单列表
撮合规则:
  · Maker: 我方 post-only 挂单排在真实队列之后（last-in-queue）
           成交条件 = 真实成交流（last_trade_price/price_change size<0）击穿我方价格
           且击穿量 ≥ 我方之前排队量；按比例部分成交
  · Taker: 吃真实簿：价格=深度加权（含真实各档大小，不打折）
           延迟: latency_ms（默认 250ms，期间簿口继续更新 → 重新评估，模拟真实竞态）
  · 撤单: 延迟后生效；期间到达的成交流仍可能成交我方（真实竞态）
  · 超时: GTD 到期自动 EXPIRED
  · 拒绝: tick 非法/最小数量/余额不足 → 模拟 CLOB 同样拒绝
```

**反例（禁止的错误做法）**：看到 ask=43¢ 就默认 100% 成交。本模拟器：买单挂出后，
只有当真实市场后续成交击穿我方价位时才可能成交——冷门价位可能整个窗口都不成交。

## 3. 资金与费用（Paper 账本）

- 起始资金 `paper_starting_equity`（默认 10,000 USDC）；
- 费用同 live 配置（默认 taker 1 bps、maker 返利 0）；滑点由 §2 自然产生；
- 结算按 §02 的「自结算 + 回收」流程：winner 以 1.00 计，可卖出价按真实簿 0.995 模拟。

## 4. 模式间一致性保证

```
同一代码断言（CI 强制）:
  · backtest/paper/live 的 FeatureStore、Probability、Strategy、Risk 为同一模块
  · Gateway 接口同构（PaperGateway 与 LiveGateway 均实现 ExchangeGateway）
  · 决策日志 schema 完全一致（复盘可比）
差异仅来自: 成交模型 vs 真实成交（这正是 paper 要验证的）
```

## 5. Paper 专属监控
- 每日自动对比「模拟成交假设 vs 真实簿可成交性」：抽样 20 单，用事后真实成交流重判我方模拟成交是否真的会发生（`paper_vs_reality` 报告）→ 校准模拟器参数（队列位置、延迟）。
- 若模拟器系统性高估成交率（>5% 偏差），暂停升级 live 流程并修正模拟器。

## 6. 运行
```
PM5HFT_MODE=paper python -m pm5hft.main            # 默认
python -m pm5hft.report paper --day today
```
Paper 阶段不需要私钥；需要的是稳定网络与 ≥99% 在线率（PTB 捕获要求窗口开始前在线，掉线窗口记入统计）。
