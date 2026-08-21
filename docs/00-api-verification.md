# 00 — Polymarket API 验证报告（2026-08 实时验证）

> 本报告所有结论均来自官方文档抓取 + 对生产 API 的实时调用验证（2026-08-14 03:12 UTC，BTC 5m 市场）。
> 实现代码必须以本报告为事实依据。

## 1. 市场结构与窗口时间（最重要）

| 项 | 结论 |
|---|---|
| 市场系列 | `btc-up-or-down-5m`（series id=10684），slug 格式 `btc-updown-5m-{t_start}` |
| t_start 推导 | `t_start = floor(now_utc / 300) * 300`（纯 UTC，无夏令时），已验证 |
| 窗口长度 | 300 秒；从 `cryptoMarketConfig.duration` 读取，不硬编码 |
| 可交易时段 | 窗口开始即可交易（`acceptingOrders=true`），窗口结束必须视为硬截止 |
| 交易所用标的 | BTC 的 ETH/SOL/XRP 等同样格式：`eth-updown-5m-…`、`sol-updown-5m-…` |
| 市场创建时间 | 事件对象会提前数小时预创建（gamma `startDate` 字段不可信，已验证其早 24h） |
| 权威时间来源 | **slug 的 t_start + duration**，不要依赖 gamma `startDate` |

实时验证数据：
```
question: Bitcoin Up or Down - August 13, 11:10PM-11:15PM ET
outcomes: ["Up","Down"]   outcomePrices: ["0.555","0.445"]
negRisk: false   acceptingOrders: true
cryptoMarketConfig: {"id":"btc-5m-twap-60","asset":"btc","duration":"5m",
                     "twapEnabled":true,"twapLookbackSeconds":60}
orderMinSize: 5   tickSize: 0.01
feeSchedule: {"exponent":1,"rate":0.07,"takerOnly":true,"rebateRate":0.2}
resolutionSource: https://data.chain.link/streams/btc-usd-twap-60s-streams
```

## 2. 结算规则（TWAP，必须按此建模）

市场 description 原文（权威结算条款）：
> Resolves "Up" if the TWAP of Bitcoin (Chainlink) **of the time range in the title** is **greater than or equal to** the price at the beginning of that range. Otherwise "Down".

关键点：
1. **结算价 = Chainlink TWAP**。**注意：结算配置历史上多次变动**——历史数据实测：8月3-6日 markets.resolution_source=`btc-usd`（现货价结算，twapLookbackSeconds=null）；8月7-13日 =`btc-usd-twap-30s-streams`（twapLookbackSeconds=30）；**8月14日起 =`btc-usd-twap-60s-streams`（twapLookbackSeconds=60，5m 与 15m 均为 60s，已用 gamma API 实时核实）**。**永远以每个市场当前的 `cryptoMarketConfig.twapLookbackSeconds` 为准，不得假设固定值**（代码已按市场自身 lookback 读取 + RTDS 同时订阅 30s/60s 两个流；历史标签重建同样按各市场 lookback 处理，见 docs/tail-evidence-11d.md §0）。
2. **开盘参考价 PTB = 窗口开始时刻的 TWAP 值**（同一 Chainlink 流在 t_start 的值）。
3. **平局算 Up**（`>=`）：模型必须把平局概率计入 Up。
4. PTB **没有公开 API**。唯一可靠获取方式：持续订阅 RTDS TWAP 流，在 t_start 采样。RTDS 无历史/重放（"no snapshot, history, or replay"）。
5. 链上 UMA 结算有分钟级延迟（实测窗口结束后 ~2.5 分钟 gamma `closed` 仍为 false），但 `outcomePrices` 会先变动；前端 ~20s 显示结果。
6. **自结算方案**：机器人自己对比 `TWAP(t_end) >= PTB` 即可在窗口结束后数秒内确定胜负，稍后与 gamma `outcomePrices=["0","1"]` 对账。

## 3. 数据获取 API

### 3.1 Gamma API（`https://gamma-api.polymarket.com`，无鉴权）
```
GET /events?slug=btc-updown-5m-1786677000
→ events[0].markets[0]：
  question, outcomes("[\"Up\",\"Down\"]"), outcomePrices, clobTokenIds(JSON字符串),
  conditionId(0x…), negRisk, acceptingOrders, closed, orderMinSize,
  orderPriceMinTickSize, cryptoMarketConfig, feeSchedule, resolutionSource,
  description, umaResolutionStatuses, bestBid, bestAsk, spread, lastTradePrice
```
- `clobTokenIds` 是**双重编码的 JSON 字符串**，需解析；index 0 = Up，index 1 = Down。
- 已关闭市场：`closed=true`，`outcomePrices=["0","1"]` 或 `["1","0"]`（胜方为 1）。
- 限速：未公开，保持 ≤1–2 req/s。

### 3.2 CLOB API（`https://clob.polymarket.com`）
公开只读：
```
GET  /time                          → Unix 秒
POST /books   body=[{token_id}]     → 批量订单簿
GET  /midpoint?token_id=            → {"mid":"0.555"}
GET  /spread?token_id=              → {"spread":"0.01"}
GET  /price?token_id=&side=buy|sell → 可成交价
GET  /last-trade-price?token_id=
GET  /tick-size/{token_id}          → tick size（注意 WS tick_size_change 事件）
GET  /fee-rate/{token_id}           → {"base_fee":1000}（实测）
GET  /prices-history?market={conditionId}&interval=1d&fidelity=N  → [{t,p}]
```

**实测陷阱**：`POST /books` 返回的 `bids[]` 是**升序**、`asks[]` 是**降序**（从中间向外排列），与文档描述相反。**代码必须自行排序**（bids 按价格降序取头部，asks 按价格升序取头部），绝不能假设 `bids[0]` 是最优买价。实测最佳买价 0.51 / 最佳卖价 0.52 与 gamma `bestBid/bestAsk` 一致（位于数组尾部）。

### 3.3 WebSocket Market 频道（公开）
```
wss://ws-subscriptions-clob.polymarket.com/ws/market
订阅: {"assets_ids":[…], "type":"market", "custom_feature_enabled":true}
心跳: 每 10s 发 "PING"（服务器回 "PONG"）
事件: book | price_change | last_trade_price | tick_size_change |
      best_bid_ask | new_market | market_resolved
```
- `price_change` 中 `size:"0"` 表示该价位被撤单移除。
- `tick_size_change` 对机器人至关重要（价格 >0.96 或 <0.04 时 tick 变化，用旧 tick 下单会被拒）。
- **订阅更新（实测，与官方 SDK market_protocol 同构）**：同一连接上不能重发初始帧或重复订阅已订阅 token（服务器回 `INVALID OPERATION`）。增量更新必须用：
  - 新增：`{"operation":"subscribe","assets_ids":[…新增…],"custom_feature_enabled":true}`
  - 移除：`{"operation":"unsubscribe","assets_ids":[…移除…]}`
  - 服务器按市场粒度扇出：同一市场的两个 token 成对增删；unsubscribe 仅在整个市场无订阅时生效。新增订阅会立即收到该 token 的 `book` 快照。

### 3.4 WebSocket User 频道（私有）
```
wss://ws-subscriptions-clob.polymarket.com/ws/user
订阅: {"auth":{"apiKey","secret","passphrase"},"markets":[conditionId],"type":"user"}
事件: trade (MATCHED/MINED/CONFIRMED/RETRYING/FAILED), order (PLACEMENT/UPDATE/CANCELLATION)
```

### 3.5 RTDS 实时参考价（公开，`wss://ws-live-data.polymarket.com`）
```
心跳: 每 5s 发 "PING"
订阅 TWAP（结算变量！）:
{"action":"subscribe","subscriptions":[
  {"topic":"crypto_prices_twap_thirty","type":"update","filters":"{\"symbol\":\"btc/usd\"}"},
  {"topic":"crypto_prices_twap_sixty","type":"update","filters":"{\"symbol\":\"btc/usd\"}"}]}
订阅现货参考价（Binance）:
{"topic":"crypto_prices","type":"update","filters":"btcusdt,ethusdt,solusdt"}
```
- TWAP 消息：`payload{symbol, value, full_accuracy_value(E18定点), timestamp(Chainlink观察时间), window_s}`。
- 精度用 `full_accuracy_value`（E18），显示值 `value` 仅供展示。
- **无快照、无历史、断线无重放** → PTB 捕获必须在窗口开始前在线。
- Python SDK 封装：`polymarket.streams.CryptoPricesChainlinkTwapSpec(window_seconds=60, symbols=["btc/usd"])`。

## 4. 交易 API（CLOB 私有）

### 4.1 鉴权
- L2：Polygon(137) 私钥 EIP-712 签名订单；API key/secret/passphrase 由 SDK 派生（`create_or_derive_api_creds` / 新 SDK 的 `AsyncSecureClient.create(private_key=…)`）。
- 老 `py-clob-client` 已归档停用；**必须用官方新 SDK `polymarket-client`（py-sdk）**。
- 敏感信息全部走环境变量。

### 4.2 下单 `POST /order`
```json
{"order":{"maker","signer","tokenId","makerAmount","takerAmount","side",
          "expiration","timestamp","salt","signatureType","signature","metadata"},
 "owner":"…","orderType":"GTC|GTD|FOK|FAK","postOnly":false,"deferExec":false}
```
- 返回 `{success, orderID, status: live|matched|delayed, makingAmount, takingAmount, transactionsHashes}`。
- **幂等性**：CLOB 无 client_order_id 字段；salt 由客户端控制 → **用确定性 salt（哈希自我们自己的 client_order_id）保证重复提交产生相同订单哈希**；同时本地 DB 记录状态防重。
- `POST /orders` 批量（≤15/请求）；`DELETE /order`、`DELETE /orders`（≤1000）、`DELETE /cancel-all`、`DELETE /cancel-market-orders`。
- 503 = cancel-only / trading-disabled / post-only 模式（有 `Retry-After`）。
- **心跳**：`POST /heartbeats` 必须定期发送，否则所有挂单会被自动撤销（死手开关）。

### 4.3 限速（按 signer 分桶，2026-07 后强制执行）
| Tier | 下单令牌/s | 下单爆发 | 撤单令牌/s | 撤单爆发 |
|---|---|---|---|---|
| Standard | 40 | 60 | 80 | 120 |
| Bronze(>$50k/30d) | 80 | 120 | 160 | 240 |
| Silver(>$100k) | 200 | 300 | 400 | 600 |
| … | | | | |
- 429 带 `Retry-After`；响应头 `Poly-RateLimit-Remaining`。批量请求按订单数计令牌，超 burst 的批量整体拒绝。
- 我们的节奏（每窗口 ≤ 几十单）远低于限制，但代码仍需令牌桶 + 退避。

### 4.4 余额 / 持仓
```
GET /balance-allowance?asset_type=COLLATERAL        → balance(6位小数基数), allowances
GET /balance-allowance?asset_type=CONDITIONAL&token_id=… → 某代币持仓
User WS trade/order 事件 → 实时成交
```

## 5. 费用（crypto_fees_v2）
- `feeSchedule {exponent:1, rate:0.07, takerOnly:true, rebateRate:0.2}`；`/fee-rate` 返回 `base_fee:1000`。
- 解释：taker 费率 ≈ 0.7 bps（rate×10^exponent），仅 taker 收费；maker 得 20% 费率返还（rebate）。
- 实现策略：费用**配置化**（默认 taker 1 bps、maker 返 0.2×taker），实盘从 `feeSchedule` 读取；Edge 计算统一用保守值。

## 6. 结论：可直接实现
1. 用 `polymarket-client`（官方 py-sdk）做鉴权/下单/订阅（MarketSpec + UserSpec + TWAP Spec）。
2. 窗口与结算全部围绕 **RTDS TWAP60 + slug 时间戳** 构建。
3. 订单簿排序、双重 JSON 编码、tick 变化、心跳/限速为已知实现要点。
4. 回测的 PTB/结算标签必须用 TWAP 近似重建（见 08-backtest-engine.md）。

## 7. 实现阶段补充实测发现（2026-08-14 冒烟运行确认）

| 项 | 实测结论 |
|---|---|
| PTB 捕获 | RTDS TWAP60 流在窗口起点给出 `obs_ts == t_start` 的精确样本，实测 PTB=63318.6671…（E18 定点串），从窗口开始到样本到达延迟 ≈ 5s |
| RTDS `crypto_prices` 订阅 | **带 `filters` 的订阅静默无消息**（文档示例不生效）；必须 `{"topic":"crypto_prices","type":"*"}` 收全量后客户端按 `payload.symbol` 过滤（实测 ~5 条/s） |
| RTDS TWAP 订阅 | 省略 filters 收全量有效（hype/sol/btc… 全部符号 ~8 条/s），客户端过滤 ✓ |
| Binance WS | `stream.binance.com:9443` 在本网络被重置（地区封锁）；**`data-stream.binance.vision` 可用**（官方公共数据端点）。原生流**禁止发送文本帧**（发 "PING" 会被 1008 Invalid request 踢掉）——依赖协议级心跳 |
| tick size | 实测窗口中途发生 `tick_size_change`：0.01 → 0.001（价格穿越 0.96/0.04 阈值时），挂单必须响应 |
| Gamma 状态机 | 窗口结束后 outcomePrices 先于 closed 变动；closed=true 约在结束后 2~3 分钟出现 |
| `GET /fee-rate/{token}` | 返回 `{"base_fee":1000}`（crypto_fees_v2 实际计费仍以配置为准） |
| 订单簿 REST 排序 | `POST /books` bids 升序/asks 降序（从中间向外），**代码必须自行排序**（已实测证实） |
