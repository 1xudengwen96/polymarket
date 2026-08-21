# 02 — 数据流（Data Flow）

## 1. 实时数据流（Paper / Live 共有）

```
Binance WS aggTrades ─┐
RTDS crypto_prices ───┤
RTDS TWAP(30/60s) ────┼──► TickRecorder/TWAPCapture ──► FeatureStore（环形缓存）
Gamma REST(轮询/事件) ─┤
CLOB Market WS ───────┼──► FeedManager ──► SharedState.book/trades/tick_size
CLOB User WS ─────────┴──► ExecutionEngine 成交对账
                                    │
                    ┌───────────────▼────────────────┐
                    │ Strategy Loop（事件驱动+周期兜底）│
                    │ 触发源：                        │
                    │  · book/trade/tick 事件          │
                    │  · TWAP 更新（结算变量变动）      │
                    │  · 100ms 周期兜底 tick           │
                    │  · 窗口边界事件（t_start/t_end） │
                    └───────────────┬────────────────┘
                                    ▼
        ProbabilityEngine ──► Fair P(Up) ──► 校准 ──► Net Edge
                                    ▼
        RiskEngine（限额检查，拒绝则记录 BLOCKED 决策）
                                    ▼
        StrategyEngine 状态机 ──► 决策 {ENTER/HEDGE/EXIT/NOOP/TAIL…}
                                    ▼
        ExecutionEngine ──► Gateway(Live|Paper) ──► 订单生命周期
                                    ▼
        Persistence（每决策一行 decision_log + 订单/成交事件）
```

## 2. 窗口生命周期（每个 5m 窗口）

```
t_start-60s   市场预注册：推导下一窗口 slug → Gamma 拉事件/tokenIds → 订阅 Market WS
t_start-2s    TWAPCapture 就绪（PTB 将从 TWAP 流在 t_start 的首个观测值采样）
t_start       窗口开盘：PTB 锁定；StrategyEngine 状态 RESET→SCANNING；
              可开始下单（book 可能刚被清扫，等待首个完整 book 快照）
t_start+1s    PTB 已捕获校验（若无 → 告警并禁用该窗口开仓，禁止用估算值实盘）
…             主循环：特征更新 → 决策 → 执行
t_end-10s     Hard Deadline 开始：禁止新开仓（含 Arb/Tail），只允许退出/对冲/撤单
t_end-5s      撤掉所有剩余挂单（避免窗口结束后无法成交的过期单）
t_end         窗口收盘：acceptingOrders=false 时刻，TWAPCapture 采样 TWAP(t_end)
t_end+~2s     自结算：Up ⇔ TWAP(t_end) >= PTB；写入 settlement 记录；更新持仓状态
t_end+数秒    若持有 winning 头寸：尝试以 ≥0.99 挂卖回收（或走 redeem 流程）；
              losing 头寸归零
t_end+分钟级  对账：Gamma outcomePrices/closed 与自结算一致；不一致→告警+调查
```

## 3. 决策数据管线（一次策略迭代）

```
输入（共享快照，全部打时间戳）:
  market: token_up/down, condition_id, tick_size, min_size, twap_lookback, fees
  price:  up_bid/ask/depth, down_bid/ask/depth, spread, last_trade, book_hash
  twap:   PTB, twap_now, twap_30s_now, 观测时间戳, staleness
  ticks:  1s/3s/5s/10s/30s/60s 收益, 短周期已实现波动率, agg buy/sell 量,
          trade_flow, 价格加速度, 反转分数
  clock:  t_into_window, t_remaining, 窗口阶段
  portfolio: 本窗口持仓(方向/数量/均价), 全局 exposure/pnl/限额状态

处理:
  1. FeatureVector 构建（稳定排序，NaN 处理）
  2. ProbabilityEngine.fair_prob(fv) → raw P(Up)
  3. calibrate(raw) → calibrated P(Up)（分桶映射，冷启动用线性插值）
  4. market_prob = up_bid+up_ask/2 对 Up 的市场隐含概率；edge = cal - market
  5. 成本项：taker fee(保守) + 滑点(按 book 深度模拟) + 延迟 + 剩余时间风险
     + 模型误差(校准误差) + risk buffer → Net Edge
  6. RiskEngine 预检（exposure/损失/持仓时间/连续亏损）
  7. StrategyEngine 状态机决策（见 04 文档）
  8. ExecutionEngine 执行或取消
  9. decision_log 落库（用户要求的全部字段，见 03 文档）
```

## 4. 订单/成交回传流

```
ExecutionEngine 发出 OrderIntent(client_order_id, token, side, price, size, tif, post_only)
   → LiveAdapter: SDK 签名 → POST /order → orderID → User WS 跟踪状态
   → PaperAdapter: PaperFillSim 按当前 book 深度/队列/延迟模拟
状态机: PENDING → LIVE(挂单) → PARTIAL → FILLED | CANCELLED | EXPIRED | REJECTED
回写: fills 表 + position 视图（按 token 聚合，本窗口维度）
异常: 提交后网络错误 → 按 order hash 查单（幂等对账）→ 未知状态则保守处理
```

## 5. 结算/资金回收流

```
自结算(几秒) → 记账（mark winning @1.0）
资金回收两条路（自动选择）:
  A. 快路: winner 在闭盘后若 book 上存在 ≥ settle_sell_min_price(默认0.995) 的 bid
     → 卖出回收（付 taker 费，立刻回笼）
  B. 慢路: 等待 gamma closed=true → redeem（赎回 $1，无市场费用，有分钟级延迟+gas）
Paper 模式: 直接按 $1.00 结算（扣除 redeem 延迟折算的机会成本参数，默认 0）
对账: 自结算 vs gamma outcomePrices，不一致记录 settlement_dispute 表
```

## 6. 回测数据流（离线）

```
历史输入:
  · Gamma/CBLOB 历史: prices-history（概率序列，1m 粒度）; data-api trades（逐笔）
  · 市场元数据: slug→窗口表（1000~5000 个窗口）
  · BTC 1s/逐笔历史: Binance data.binance.vision aggTrades 月包
  · TWAP 标签重建: 用 Binance aggTrades 自算 TWAP60 近似（敏感度分析 ±2s/±5s 窗口）
       └─ 或直接购买/申请 Chainlink Data Streams 历史（可选，提高标签精度）
重放: 逐窗口按事件时间推进（严格禁止未来数据）→ 同一套 FeatureStore/Engines
     → PaperFillSim（含队列/延迟/滑点/部分成交）→ 全链路指标（见 08 文档）
```
