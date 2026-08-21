# 05 — Probability Engine（概率引擎）

## 1. 目标变量（TWAP 时代，与 oracle 完全对齐）

```
P(Up) = P( TWAP_L(t_end) >= PTB )
其中 L = cryptoMarketConfig.twapLookbackSeconds（当前 BTC 5m = 60s）
PTB  = TWAP_L 在 t_start 的观测值
```

**不是**预测「BTC 收盘价涨跌」，而是预测 Chainlink TWAP 终值与 PTB 的大小关系。
训练/回测标签一律用 TWAP（无法获得官方历史时用 Binance aggTrades 重建近似，见 08 文档）。

## 2. 特征集（FeatureVector，固定顺序/命名，含 NaN 策略）

| 类 | 特征 | 说明 |
|---|---|---|
| 距离 | `norm_distance` | (TWAP_now - PTB) / PTB × 1e4（bps） |
| | `twap_to_ptb_dist_30s` | 30s TWAP 与 PTB 距离（捕捉更短惯性） |
| 时间 | `remaining_s`, `into_window_s`, `remaining_frac` | 剩余时间为核心交互项 |
| 收益 | `ret_1s..60s`（6个） | 参考价收益率 |
| 波动 | `rv_5s, rv_30s, rv_60s` | 已实现波动率；`vol_ratio_short_long` |
| 量能 | `vol_1s, vol_10s, vol_60s` | Binance 成交量（对数化） |
| 订单流 | `agg_buy_5s, agg_sell_5s, agg_buy_30s, agg_sell_30s` | 攻击性买卖量 |
| | `tfi_5s, tfi_30s` | Trade Flow Imbalance = (buy-sell)/(buy+sell) |
| | `cvd` | 累计成交量差（窗口内标准化） |
| 簿口 | `obi_levels_3, obi_levels_10` | Order Book Imbalance |
| | `up_spread, down_spread, mid_microprice` | |
| 价格行为 | `accel_5s` | 价格加速度 |
| | `reversal_score` | 相对极值的反转倾向（正则化后） |
| 市场 | `pm_mid_up, pm_last_up, pm_1m_change_up` | Polymarket 自身价格（市场参与者信念） |
| 交互 | `dist × remaining_frac` 等 3~5 个精选交互项 | |

全部特征**窗口内滚动标准化**（z-score 用过去 N 窗口统计，防前视）。缺失策略：NaN 显式编码 + 特征有效性位。

## 3. 模型（梯度升级路线，每个都先于生产验证）

| 阶段 | 模型 | 说明 |
|---|---|---|
| P0 | **逻辑回归 / 梯度提升（LightGBM）** | 可解释、训练快；分桶校准直接 |
| P1 | 分时间模型 T-120/T-60/T-30/T-20/T-10/T-5 各一个子模型 | 剩余时间条件化（用户 §13） |
| P2 | 在线更新（增量 LightGBM / 在线逻辑回归） | 慢漂移适应 |

研究任务（回测阶段必须产出报告）：
- 各剩余时间桶的真实胜率曲线与模型置信度的关系；
- **最后 10~20 秒的统计显著行为检验**（TWAP 制下：末段影响力被 60s 窗口摊薄，需定量验证而非假设）；
- 特征重要性 + SHAP 前 20；
- 「98~99.9¢」尾差市场：模型在极端区间的校准质量（决定 TAIL_CAPTURE 是否可用）。

## 4. 校准（§14 必做）

分桶（模型输出概率维度）：
```
[0.50-0.55, 0.55-0.60, 0.60-0.65, 0.65-0.70, 0.70-0.75, 0.75-0.80,
 0.80-0.85, 0.85-0.90, 0.90-0.95, 0.95-0.97, 0.97-0.98, 0.98-0.99, 0.99-1.00]
```
每桶记录：n、胜场、实际胜率、Brier、ECE 贡献（见 `calibration_buckets` 表）。
使用规则：
- **Platt / Isotonic 校准器**按时间窗滚动拟合（训练集时序切分，禁前视）；
- 冷启动（样本 < `min_bucket_n`，默认 200）：原始概率 × 保守收缩因子（默认 0.85），并强制通过 Risk Buffer；
- **99% 校准陷阱**：若 0.99+ 桶实际胜率 < 市场价 → TAIL_CAPTURE 该桶直接禁用（代码级）。

## 5. Fair → Net Edge 管线（与 §3/§15 一致）

```
fair_prob → calibrate → cal_prob
market_prob = mid(up_bid, up_ask) 或成交价合成
gross_edge = cal_prob - up_ask（买 Up 时）
net_edge   = gross_edge
             - taker_fee(保守)
             - slippage_est（按深度加权模拟，最少 1 tick）
             - exec_risk（延迟/撤单重试失败概率 × 代价，默认 0.3%）
             - time_risk（剩余时间越短扣越多：默认按 (60/remaining)^0.5 × 0.2%）
             - model_err（该校准桶 |actual - predicted| 的 1σ，冷启动用 3%）
             - risk_buffer（全局 0.5% + 桶级）
```
`net_edge > entry_min_net_edge` 才可开仓（阈值见 04 文档）。
Edge 计算全部用 Decimal，禁止浮点。

## 6. 与回测/实盘的一致性
- 特征计算代码 = 回测 = paper = live 同一模块（`features/` 包）；
- 校准器/模型权重版本化（`model_version` 写入 decision_log）；
- 模型只能使用 t 时刻及之前数据；训练/验证/测试按窗口时间顺序切分（前 60% 训练、20% 验证、20% 测试，滚动 walk-forward）。
