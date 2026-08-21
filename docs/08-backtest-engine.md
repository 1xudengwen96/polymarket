# 08 — Backtest Engine（回测引擎）

**回测未达标前禁止实盘（铁律）。目标规模：1000~5000 个 BTC 5m 窗口。**

## 1. 数据管线

### 1.1 市场元数据（100% 真实）
- 从 Gamma 拉历史事件：`btc-updown-5m-{t_start}` 逐个查询（节流 1 rps，可断点续传），
  或先拉 series（`btc-up-or-down-5m`）事件列表页；
- 记录：token ids、condition id、tick/min size、`cryptoMarketConfig`（确认 TWAP 窗口）、fees。
- 已有公开数据集可作补充/校验：HuggingFace `Solal9/polymarket-crypto-updown-binary`、`krish301/polymarket-raw-5m`（使用时校验字段与官方 API 一致性）。

### 1.2 概率价格序列
- CLOB `GET /prices-history?market={conditionId}&interval=1d&fidelity=N` → 每窗口概率曲线；
- `data-api.polymarket.com/trades?market=…` 逐笔成交（订单流特征回放）；
- 订单簿逐档历史**没有公开 API** → 回测簿口用「成交流 + 概率序列」重建近似簿（见 §3），并做保守化处理。

### 1.3 BTC 价格与 TWAP 标签（核心难点）
- 官方 Chainlink TWAP 历史无公开 API（需赞助 Data Streams 凭证，可选项）；
- **默认方案**：Binance `data.binance.vision` aggTrades 月包（1s 内精度）→ 自算 `TWAP60(t_start)`、`TWAP60(t_end)`；
- 标签重建校验：对**已结算窗口**用自算 TWAP 判断胜负，与 gamma `outcomePrices` 对账，
  不一致率必须 < 2%（否则该段数据作废）——这是防「标签漂移」的硬校验；
- 敏感性分析：TWAP 窗口 ±5s/±10s 重算标签，评估策略对标签误差的脆弱性（脆弱则不投产）。

## 2. 回放架构

```
事件驱动回放（与 live 同一代码路径）:
  时间推进: 按 [binance ticks, polymarket trades, price_history] 合并的时间轴
  每个窗口:
    t_start: 重建 PTB（自算 TWAP）→ 注册市场
    逐事件: TickRecorder 特征更新 → ProbabilityEngine → StrategyEngine → RiskEngine
            → ExecutionEngine → BacktestFillSim（同 PaperFillSim，参数可调）
    t_end:   结算（自算 TWAP 标签）→ 记账
  输出: 与 live 完全相同的 DB 行（decision_log/orders/fills/positions/settlements）
```

## 3. 成交模拟（必须保守，见 §18）

| 机制 | 模拟方式 |
|---|---|
| Maker 限价单 | 只有**对手主动成交流击穿价格**才成交；按时间优先级队列，我们排在队尾（`queue_position=last`）；部分成交按击穿量 |
| Taker 单 | 用重建簿深度加权：ask 各档 × 深度系数 `depth_confidence`（默认 0.5，因重建簿不准）；FOK 按可支撑量判定 |
| 滑点 | taker 按深度加权价 vs 目标价之差 |
| 手续费 | 按配置（默认 taker 1 bps、maker 返利 0） |
| 延迟 | 事件→决策→下单→成交加入 `latency_ms`（默认 250ms，可扫参 100/250/500/1000） |
| 撤单 | 撤单请求到生效加延迟；期间可能被成交（与真实竞态一致） |
| 簿口移动 | 概率序列 + 成交流外推；未观察档位按保守缩量 |

**Lookahead 防护（代码级强制）**：
- 回放指针 `now` 之后的事件一律不可见（事件队列按时间排序，断言 `ev.ts >= now`）；
- 特征只能用 `<= now` 的数据；标签只用于结算，不进特征；
- 训练/验证/测试按窗口时间切分；校准器拟合集与评估集严格分离。

## 4. 指标（§18 全套，与 live Reporter 同一函数）

```
Win Rate, Avg Win/Loss, Expectancy, Profit Factor, Max Drawdown,
Sharpe, Sortino, Capital Efficiency, Turnover, Avg Holding Time,
Initial Entry Edge(平均/分位), Hedge Success Rate（对冲尝试成功率）,
Complete Set Success Rate, Tail Capture Success Rate, Arbitrage Success Rate,
逐桶校准表（Brier/ECE），按小时/星期分段稳定性
```

## 5. 验收门槛（达到才进入 Paper 阶段）

- 样本 ≥ 1000 窗口，测试集（最近 20% 时间）：
  - Expectancy > 0（扣费后）
  - Profit Factor ≥ 1.15
  - Max Drawdown ≤ 3%（equity）
  - 与标签敏感性：±5s TWAP 扰动后结论不变号
  - 延迟扫参 100→500ms 结论不翻转
  - 单腿收益不依赖任一单一模块（Arb/Tail 贡献 ≤ 30% 总 PnL，防“回测靠罕见套利活”）
- 输出 `docs/backtest-report.md`（含全部曲线图）。

## 6. 回测运行方式
```
python -m pm5hft.backtest run --windows 3000 --from 2026-06-01 --to 2026-08-01
python -m pm5hft.backtest report --run-id xxx
```
（先跑 200 窗口冒烟，再全量。）
