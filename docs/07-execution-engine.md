# 07 — Execution Engine（执行引擎）

## 1. 订单模型

```
OrderIntent: client_order_id(uuid7), token_id, side(BUY/SELL), price, size,
             tif(GTC|GTD|FOK|FAK), post_only, expires_at, meta{strategy_module, window}
```

| 场景 | 订单类型 | 说明 |
|---|---|---|
| Cheap Entry / Hedge / Tail | **Post-Only GTD 限价**（默认） | 吃 maker 返利、无 taker 费；挂不上就重评，绝不追价 |
| 紧急退出（末段/止损） | FAK（能成交多少算多少） | 深度加权判断预期成交 |
| 确定性套利（Arb 两腿） | FOK 双提交 | 单腿失败→秒撤另一腿 |
| 正常获利退出 | 先挂 post-only 卖单，`exit_patience_s`（默认 3s）后转 FAK | 优先省费，其次速度 |

## 2. 幂等与安全（§16）

- **client_order_id**（uuid7，我们生成）→ 写入 `orders` 表主键；所有重试复用同一 id。
- **确定性 salt**：`salt = sha256(client_order_id) mod 2^62` → 相同意图重复签名产生相同订单哈希；重复提交被 CLOB 幂等去重。
- 提交协议：
```
POST /order 超时/网络错误 → 不盲目重试：
  ① 先按 order_hash/最近订单查询（GET 单订单 / User WS 对账）
  ② 未知 → 进入 UNKNOWN 状态，等待 User WS trade/order 事件裁决（默认 5s）
  ③ 仍未知 → 保守处置：若为买单且可能已成交 → 查 positions；否则取消意图
```
- 所有状态机转移写 DB（崩溃恢复从 DB 重建）。

## 3. 挂单管理循环（每个窗口）

```
maker 订单生命周期:
  LIVE → (book 变化) 每 500ms 评估一次:
    · 价格已偏离 target ± 2 tick → REPLACE（撤旧挂新，同 client_order_id 新版本）
    · 剩余时间 < GTD 到期 → 自动失效
    · 已部分成交 → 保留剩余（按 hedge 目标重新计算剩余量）
    · 目标达成/状态机离开该态 → CANCEL
  EXPIRED/REJECTED → 记录原因，回策略层（不自动重挂，由策略重评后决定）
```

## 4. 限速管理（对照 2026-07 新 token-bucket 政策）

- 本地令牌桶镜像 CLOB 规则：order 桶、cancel 桶分别建模（Standard 40/60、80/120）；
- 每次响应解析 `Poly-RateLimit-Remaining` 校准本地桶；
- 429 → 按 `Retry-After` 退避；批量下单按条数计 token，超 burst 的批量拆小；
- 我们的实际节奏（每窗口 ≤ 数十单）远低于限额，代码仍必须实现，因为「撤单风暴」场景会瞬间打爆 cancel 桶（一次 `cancel-all` 只花 1 token，优先用）。

## 5. 心跳与看门狗

- CLOB `POST /heartbeats` 每 10s（失联 → 交易所自动撤全部挂单，兜底安全）；
- 本地看门狗：主循环 3s 无心跳 → `DELETE /cancel-all` + 进程重启（Supervisor）。

## 6. 与市场时钟的联动（已在 04 §9 定义）

- 强制 `t_end-5s` 全撤（每窗口定时任务，不可跳过）；
- `tick_size_change` 事件 → 立即撤掉该 token 全部挂单（价格可能已非法），重评后用新 tick 重挂；
- 503 cancel-only / trading-disabled → 停止提交新单，仅保留撤单能力。

## 7. Gateway 抽象（paper/live 同接口）

```python
class ExchangeGateway(Protocol):
    async def submit(self, intent: OrderIntent) -> SubmitResult      # ack 即回
    async def cancel(self, client_order_id) -> CancelResult
    async def cancel_all_market(self, condition_id) -> None
    async def reconcile(self) -> None                                 # 与交易所对账
    # 事件回调（成交/状态变化）→ 推给 ExecutionEngine 事件队列
```
- `LiveGateway`：polymarket-client SecureClient + CLOB REST + User WS；
- `PaperGateway`：PaperFillSim（见 09 文档）；
- ExecutionEngine 完全不知道自己在哪种模式（模式只在 Gateway 与风控参数里体现）。

## 8. 失败处理矩阵

| 故障 | 处理 |
|---|---|
| 下单 4xx（tick 非法/余额不足/ban） | 记录 REJECTED，熔断计数；tick 类先刷新 tick 再重评 |
| 下单 5xx/超时 | 幂等对账协议（§2），指数退避重试 ≤ 2 次 |
| 撤单失败（已成交竞态） | 视为可能已成交 → 等 User WS 裁决，绝不再下反单对冲 |
| User WS 断线 | REST 轮询对账（open orders/positions），恢复后重订阅 |
| 结算后仍有挂单 | 强制撤 + 告警（正常流程不可能发生） |
