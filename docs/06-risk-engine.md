# 06 — Risk Engine（风险引擎）

## 1. 设计原则
- **每笔决策前强制预检**（pre-trade check），任何一条不通过 → 拒绝并写 `reject_reason`。
- 限额分两级：**账户级**（跨资产/跨窗口）与 **窗口级**（单市场）。
- 熔断是**单向棘轮**：触发后本窗口/本小时/本日不再开新仓，只允许减仓/撤单（`kill_switch` 例外：直接撤全单）。
- 所有额度在 DB 持久化，进程重启后不丢。

## 2. Exposure 分类（对应 §15）

| 指标 | 定义 | 限制（默认值） |
|---|---|---|
| Directional Exposure | Σ(单边名义)（未对冲净额按方向） | ≤ `max_unhedged_exposure` = equity×1.0%（默认 100 USDC @10k） |
| Initial Exposure | 首次建仓名义（单窗口） | ≤ `max_initial_exposure` = 50 USDC |
| Unhedged Exposure | 有方向风险的名额 = Σ(Up-Down 净值) | ≤ equity×1.0% |
| Complete Set Exposure | 两腿齐全部分的名义 | ≤ `max_complete_set_exposure` = equity×5% |
| Market Exposure | 总占用资金（所有窗口所有腿） | ≤ `max_market_exposure` = equity×10% |
| Total Exposure | 含 TAIL/ARB 的全局占用 | ≤ equity×10% |

窗口级：单窗口所有腿总名义 ≤ `max_window_notional`（默认 200 USDC）。

## 3. 亏损与回撤限额

```
max_daily_loss    = equity × 1.0%（默认 100 USDC）→ 触发后当日禁开仓，次日人工确认重置
max_hourly_loss   = equity × 0.5%
max_consecutive_losses = 6 个窗口（连续结算亏损）→ 冷却 cooloff_windows = 12（默认）
max_holding_time  = 1 个窗口（持仓绝不跨窗口滚动，窗口结束即结算，铁律）
max_drawdown      = equity × 3%（从峰值回撤）→ 全局停机（kill_switch，撤全单+停开仓）
```

PnL 口径：**已实现 PnL**（结算/退出落袋）与 mark-to-market 双轨；限额用已实现+锁定利润。
费用、滑点全部计入 PnL（无“毛估”）。

## 4. 硬编码铁律（不可配置）
1. 禁止 Martingale：同窗口同方向加仓 → 直接拒绝（`MARTINGALE_BLOCK`）。
2. 禁止亏钱对冲：complete_set_cost + 2×taker_fee ≥ 1 → 拒绝（`HEDGE_LOSS_BLOCK`）。
3. 禁止跨窗口滚动：任何窗口持仓在 t_end+30s 后必须已结算/赎回/归零，否则告警并强制标记。
4. 禁止超限成交：订单提交前用可执行价×数量重新算 exposure；FOK 成交后立即对账，超限部分立刻市价平掉（罕见兜底）。
5. PTB 缺失时：该窗口禁开仓（`PTB_MISSING_BLOCK`），但允许撤单与退出。

## 5. 熔断器（Circuit Breakers，独立于限额）

| 熔断 | 触发 | 动作 | 复位 |
|---|---|---|---|
| `book_stale` | 簿口 hash 不变 > 1.5s 或与服务器时间差 > 2s | 禁开仓+撤挂单 | 簿口恢复 |
| `twap_stale` | TWAP 观测 > 5s 无更新（窗口内） | 禁开仓，警报 | 流恢复 |
| `tick_storm` | 1s 内 tick_size_change ≥ 3 次 | 撤单，冷却 5s | 冷却结束 |
| `fill_anomaly` | 成交价偏离请求价 > 3 tick（非 FOK） | 撤该市场单，调查 | 人工/超时 |
| `api_error_rate` | 下单错误率 > 20%（滑动 20 单） | 冷却 60s | 冷却结束 |
| `cancel_only` | CLOB 返回 cancel-only/trading-disabled | 停开仓，仅撤单 | 轮询恢复 |
| `max_drawdown` | 见 §3 | kill_switch | 人工确认 |

## 6. 风控状态机（账户级）

```
NORMAL ──触发限额/熔断──► COOLDOWN（本窗口禁开仓，允许退出/撤单）
NORMAL/COOLDOWN ──drawdown/大额异常──► KILL_SWITCH（cancel-all + 停开仓，需人工解锁）
```

## 7. 心跳与死手开关（交易所侧）
- CLOB `POST /heartbeats` 周期（默认 10s，与限速文档一致）——**机器人失联时交易所自动撤全部挂单**，这是最后一道防线。
- 本地 watchdog：Supervisor 检测主循环心跳 > 3s 未跳 → 主动 cancel-all + 重启。

## 8. 配置文件节选（config/risk.yaml）

```yaml
account_equity: auto            # auto=从 balance-allowance 读取；paper 用 paper_starting_equity
max_initial_exposure: 50
max_unhedged_exposure_pct: 0.01
max_complete_set_exposure_pct: 0.05
max_market_exposure_pct: 0.10
max_window_notional: 200
max_daily_loss_pct: 0.010
max_hourly_loss_pct: 0.005
max_consecutive_losses: 6
cooloff_windows: 12
max_holding_time_s: 330
max_drawdown_pct: 0.03
paper_starting_equity: 10000
```
