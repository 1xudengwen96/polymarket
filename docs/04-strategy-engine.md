# 04 — Strategy Engine（策略引擎）

目标不是预测 BTC 涨跌，而是：「**拿便宜筹码 → 等待重定价或低成本对冲 → 退出/锁定**」。
四条铁律（编码为不可配置的硬约束）：
1. **禁止 Martingale**：绝不因持仓浮亏而加仓同一方向。
2. **禁止无限补仓**：每窗口每方向最多 `max_entries_per_side`（默认 1）次建仓动作。
3. **禁止亏钱对冲**：Complete Set Cost ≥ 1 时禁止买对边（扣除费用后必须仍盈利）。
4. **禁止假设**：不假设均值回归/99¢稳赚/末段反转/必有更好对冲价——一切由校准数据说话。

## 1. Position State Machine（每窗口独立状态机）

```
STATE_0 NO_POSITION ──(edge>阈值 & 窗口阶段允许 & 风险通过)──►
STATE_1 SEEKING（扫描簿口，挂 post-only 限价单）
STATE_2 SMALL_INITIAL（已建小仓；进入 REPRICING 监控）
STATE_3 WAIT_REPRICING（持续评估两条腿）
   ├──► STATE_4A EXIT_PROFIT：当前可成交价 ≥ entry + min_profit（≥ 目标）→ 卖出/吃单退出
   ├──► STATE_4B HEDGE：对边可成交价使 Complete Set Cost < 1 - fees - locked_margin → 补对边
   ├──► 时间止损/价格止损 → EXIT_STOP（接受亏损，按规则退出）
   └── t_end 前未触发 → FINAL_EXIT（能卖则卖，否则持有到结算）
STATE_5 COMPLETE_SET（Up qty ≈ Down qty 且总成本 < 1）
STATE_6 LOCKED_PROFIT（无方向风险；等结算/赎回，或提前以 ≥0.995 卖出组合）
STATE_7 SETTLEMENT → 记账 → RESET（窗口重置）
```

转移守卫（每个转移都要过的检查）：
- **风险守卫**（RiskEngine）：未达任何熔断限制。
- **时间守卫**：`t_remaining > hard_deadline`（默认 10s，见 02 文档；对冲宽限到 `hedge_deadline` 默认 5s）。
- **价格守卫**：仅用「可执行价格」计算（挂单用 post-only 价格、吃单用 ask 及深度加权）。

## 2. 模块一：Cheap Entry（便宜筹码）

触发条件（全部满足）：
```
net_edge(cal_prob - up_ask) > entry_min_net_edge          # 默认 4%
cal_prob - market_prob > entry_min_gross_edge            # 默认 6%
t_into_window >= entry_min_age_s                         # 默认 5s（等首个稳定簿）
t_remaining   >= entry_min_remaining_s                   # 默认 90s
book 非空且深度可执行、无 tick 变动风暴、PTB 已捕获
```

下单方式（默认 maker 优先）：
- 挂 post-only GTD 限价单，价格 = `min(fair_value*(1-maker_safety), up_bid+1tick)`，GTD 至 `min(t_end-15s, now+entry_order_ttl)`；
- 超时未成交 → 撤单并重评（repricing 循环），**绝不追价加仓**；
- 若 `entry_take_allowed=true` 且 net_edge ≥ `taker_entry_min_edge`（默认 8%）可 FOK 吃单。

仓位大小（初始必须小，全部配置化）：
```
initial_notional = equity * initial_position_pct      # 默认 0.2%（10k 账户 = 20 USDC）
clamp: [min_initial_notional(5), max_initial_notional(50)]
且 initial_notional ≤ max_initial_exposure ≤ max_unhedged_exposure
股数 = round_to_min_size(initial_notional / price)
```

## 3. 模块二：Repricing Engine（重定价监控）

持仓期间每个决策周期计算：
```
up_mkt   = 可执行卖价（我方退出价）或 mid
down_mkt = 可执行买价（对冲成本）
Δfair = cal_prob_now - cal_prob_entry
Δmkt  = up_mkt - avg_entry_up
btc 动量/波动/簿口/订单流变化（特征漂移检测）
```

退出分支（先到先得）：
```
EXIT_PROFIT: up_mkt >= avg_entry + exit_min_profit_ticks 且
             realized_pnl_after_fee ≥ max(exit_min_profit_usd, 0.6×expected_edge_pnl)
EXIT_STOP:   up_mkt <= avg_entry - stop_loss_ticks（默认 -6 tick） 或
             t_remaining ≤ stop_remaining_s（默认 15s，未对冲的裸仓强制处理）
EXIT_SIGNAL: cal_prob - 修正后市场概率 < -exit_confidence_turn（默认 -8%，模型反转离场）
```
原则：**不为等对冲而强留**——重定价到位就走，落袋优先。

## 4. 模块三：Hedge Opportunity（低成本对冲）

仅当存在方向持仓且未 HEDGED：
```
complete_set_cost = avg_entry_held + executable_opposite_ask（深度加权）
locked_gross      = 1 - complete_set_cost
net_locked        = locked_gross - 2*taker_fee - hedge_safety_margin(默认 1.5%)
可执行 ⇔ net_locked > hedge_min_locked_pct（默认 3%）
```
- 对冲数量 = min(持仓数量, 对边深度可支撑数量)；允许部分对冲。
- 对冲单同样 maker 优先：挂 post-only 买单价 = `min(1 - avg_entry_held - net_locked, down_bid+1tick)`，GTD 至 hedge_deadline。
- **绝不**在 complete_set_cost ≥ 1 - 费用时买对边（铁律 3）。
- 对冲失败 → 交给 Risk/时间守卫（持有到窗口结构退出或结算）。

## 5. Complete Set Lock

```
Up qty == Down qty 且 total_cost < 1 → state=COMPLETE_SET
记录: locked_profit = payout - cost - fees; roi; capital_efficiency = profit/capital_deployed;
      time_to_settlement
处置: 提前以 ≥ settle_sell_min_price(0.995) 挂卖两腿（若簿口可成交且净赚 >
      等待结算赎回的价值 → 卖；否则持有等赎回）。
```

## 6. 模块四：TAIL_CAPTURE（98~99.9¢ 尾差）

- 独立于主状态机（可同时存在）。
- 触发：`market_price ∈ [0.98, 0.999)` 且 `cal_prob > market_price + tail_capture_buffer`（默认 1.5%），
  扣除 taker 费 + 滑点 + 尾部风险 buffer（默认 0.5%）后 Net Edge > 0。
- 仓位：`tail_position_pct`（默认 0.1% equity，上限 `max_tail_notional` 默认 20 USDC）。
- 禁止：单纯「看到 99¢ 就买」；必须过校准分桶校验（99¢ 分桶真实胜率必须支撑）。
- 头寸与主策略分别计 exposure；窗口尾部（t_remaining<30s）由 `tail_min_remaining_s` 守卫。

## 7. 模块五：TAIL_HEDGE（尾部保险）

- 仅当持有 ≥98¢ 的 TAIL_CAPTURE 头寸（或 Complete Set 不平衡的极端方向敞口）。
- 保险成本 ≤ `tail_hedge_max_cost_pct × 主头寸预期利润`（默认 20%）。
- 买 1~5¢ 对边，数量 = 按条件数学计算：`qty_hedge = 头寸名义 × insured_fraction / 对边ask`，
  上限 `max_tail_hedge_notional`（默认 5 USDC）。
- 若对边价格 > `tail_hedge_max_price`（默认 5¢）→ 放弃保险（不为保险付出过高价格）。

## 8. 模块六：Pure Arbitrage（Up + Down < 1）

- 独立扫描每次簿口更新：
```
exec_up_ask(qty)   = 深度加权可成交 Up 卖价
exec_down_ask(qty) = 深度加权可成交 Down 卖价
total = up + down; profit = 1 - total - 2*taker_fee - arb_slippage_buffer(默认 0.3%)
```
- 执行条件：`profit > arb_min_net_pct`（默认 0.8%）且 `qty ≥ min_arb_qty`（默认 10 股）、
  两腿同时 FOK/FAK 提交（先 Up 后 Down，间隔 <100ms），单腿失败 → 立即撤另一腿并记录 partial_arb。
- 数量按两簿 min 可支撑深度 × 0.5 保守系数；单窗口总 arb 名义 ≤ `max_arb_notional`（默认 50 USDC）。
- 资金占用：两腿名义（≈2×qty），需通过 RiskEngine 的 `max_market_exposure`。
- 已知现实：此类机会在 5m 窗口罕见且寿命 <1s；该模块必须零延迟订阅驱动，且被撤单风暴熔断保护。

## 9. 与窗口时钟的耦合

| 阶段 | 允许动作 |
|---|---|
| t_start ~ t_start+5s | 禁止（等 PTB + 稳定簿） |
| 正常期（至 t_end-90s） | 全部模块 |
| t_end-90s ~ t_end-30s | ENTER 需 ≥ 高阈值 edge；HEDGE/EXIT/TAIL_HEDGE 允许 |
| t_end-30s ~ t_end-10s | 禁止新 ENTER/TAIL_CAPTURE/ARB；只 EXIT/HEDGE |
| t_end-10s ~ t_end-5s | 仅 EXIT（市价/急卖）、撤单 |
| t_end-5s ~ t_end | 只撤单，禁止一切下单 |
| t_end 之后 | 结算/回收/对账 |

## 10. 配置表（节选，全部在 config/strategy.yaml，值均可调）

```yaml
entry:
  initial_position_pct: 0.002
  min_initial_notional: 5
  max_initial_notional: 50
  entry_min_net_edge: 0.04
  entry_min_gross_edge: 0.06
  entry_min_age_s: 5
  entry_min_remaining_s: 90
  entry_order_ttl_s: 20
  max_entries_per_side: 1
exit:
  exit_min_profit_ticks: 3
  stop_loss_ticks: 6
  stop_remaining_s: 15
  exit_confidence_turn: 0.08
hedge:
  hedge_min_locked_pct: 0.03
  hedge_safety_margin: 0.015
  settle_sell_min_price: 0.995
tail_capture:
  enabled: true
  buffer: 0.015
  tail_risk_buffer: 0.005
  tail_position_pct: 0.001
  max_tail_notional: 20
  tail_min_remaining_s: 30
tail_hedge:
  enabled: true
  max_cost_pct: 0.20
  max_price: 0.05
  max_notional: 5
arb:
  enabled: true
  min_net_pct: 0.008
  slippage_buffer: 0.003
  min_qty: 10
  max_notional: 50
```

## 11. 反马丁格尔的代码级保证
- `position.qty(方向) 单调非增` 断言：除 HEDGE 补对边外，任何「加仓」路径在 RiskEngine 硬编码拒绝；
- 对边补仓金额上限 = 已持仓名义 × 1.0（只能补到 Complete Set，不能超额）；
- 每个窗口的 entry 动作计数持久化（DB），重启后不重置。
