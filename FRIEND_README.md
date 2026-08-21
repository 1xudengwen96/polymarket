# pm5hft 部署与修改指南（给接手的朋友）

Polymarket 加密资产 5 分钟 Up/Down 高频交易机器人。唯一在跑的实盘策略是
**Tail Capture（尾部买方）**：ask ∈ [0.98, 0.999) 时用 maker（post-only）挂单
买热门侧，每笔 5 USDC，**持有到结算**，绝不中途出场。

> 出场路径被显式封死：纸面 375 笔反事实证明持有到结算优于所有出场变体
> （+3tick 锁定 / 末段出场 / 冲顶出场全部更差）。天鹅（尾部翻盘）风险由
> 入场端管理（校准桶禁用 + 小仓位 + 日亏限额），不由出场管理。

---

## 1. 环境要求与安装

- Python 3.11+（开发环境 3.12），Windows 或 Linux
- 依赖安装（在包根目录）：

```bash
pip install -e .
# 可选：开发/测试
pip install -e ".[dev]"
```

网络要求：可访问 polymarket.com / binance / Cloudflare（国内部署会间歇性被
网络层重置连接——bot 有自动重连 + REST 簿口轮询自愈，但**强烈建议部署在
海外服务器**，见 docs/live-runbook.md §3 爱尔兰 AWS 方案）。

## 2. 先跑纸面盘（无真钱，熟悉一切）

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python -m pm5hft.main --log-level INFO
```

另开一个窗口起面板：

```powershell
python -m pm5hft.dashboard --port 8090 --db data/pm5hft.db
```

浏览器打开 http://127.0.0.1:8090。纸面盘权益默认 1000，用真实 CLOB 流
（含 REST 实时簿口）决策、用纸面撮合模拟成交。

重置纸面数据库：`python scripts/reset_paper_db.py`

## 3. 实盘启动（三步，缺一不可）

1. **充值**：Polymarket 账户充 USDC（建议 ≥95，留 Polygon gas 余量）。
2. **开双开关**：
   - `config/live.yaml` → `allow_live: true`（默认 false，防止误开）
   - 在 `scripts/launch_live.ps1` 第 9 行填入 EOA 私钥
     **（此文件绝不要提交 git、绝不要发给别人）**
3. **启动**（Windows PowerShell）：

```powershell
.\scripts\launch_live.ps1
```

启动日志应依次出现：
`live client created` → `startup safety: cancelled all open orders` →
`live equity loaded from collateral balance` → `REST book polling enabled`

启动时会把交易所上残留的全部挂单撤掉（崩溃兜底），并从链上读取真实 USDC
余额作为风控基数（读不到就拒绝启动，绝不带占位权益交易）。

实盘面板（另开窗口）：

```powershell
python -m pm5hft.dashboard --port 8091 --db data/pm5hft-live.db
```

## 4. 运行前必读（实盘验证过的重要规则）

1. **bot 运行期间绝不要手动交易同一账户**：bot 的持仓/风控账本看不到你的
   手动单，账目会脱节。要手动干预先停 bot。
2. **每次重启实盘**：Ctrl+C 停掉旧进程再启动。重启会撤全部挂单，并重新
   读取余额；当前窗口的簿口几秒内恢复（REST 轮询 + 订阅快照）。
3. **时间同步**：交易所要求签名时间戳与 UTC 误差 <60s。部署后先对时
   （Windows: `w32tm /resync`；Linux: `chronyd`/`timedatectl`）。
4. **断电保护**：机器休眠会停掉一切。服务器/桌面都要关休眠、设来电自启。
5. **不要改的策略铁律**（风险引擎硬编码）：
   - 禁止 Martingale / 同向加仓 / 亏钱对冲 / 强留等对冲
   - 尾单出场路径封死（持有到结算）——数据结论，勿轻易重开

## 5. 关键配置

| 文件 | 内容 |
|---|---|
| `config/strategy.yaml` | `tail_capture.*`（触发区 0.98-0.999、每笔 notional、模型闸门开关）；`books.rest_poll_*`（REST 实时簿口轮询）；`entry.*`（方向性入场已禁用=99）；`exit.*`（只影响非 tail 仓位）|
| `config/assets.yaml` | 资产开关（现仅 btc/eth enabled；xrp/sol/bnb/doge/hype 停牌）|
| `config/live_risk.yaml` | 实盘风控档：单笔上限、日亏 12%、小时 6%、回撤 20%、连亏 6 冷却 |
| `config/live.yaml` | 实盘总开关 |
| `config/probability.yaml` | 模型工件路径（artifacts/logreg_v1.json / logreg_v1_live.json）|

修改策略最常改的地方：`tail_capture.min_notional`（每笔 USDC）、
`tail_capture.tail_min_remaining_s`（最晚入场秒数）、
`assets.yaml` 资产开关、`live_risk.yaml` 限额。

## 6. 日常运维

- 预检（只读，检查账户/配置/可达性）：`python scripts/live_preflight.py`
- 健康报告：`python scripts/health_report.py`
- 盯面板：纸面 8090 / 实盘 8091
- 日志：`logs/live-YYYYMMDD-HHMM.log`（每 30s 一条 status 摘要）
- 关键日志事件：`live fill`（成交，带 role/side/price/qty）、
  `GTD expired`（挂单到期撤单）、`window settled`（结算+PnL）、
  `registry pruned`（窗口轮换清理）

## 7. 测试

```bash
python -m pytest tests -q
```

## 8. 部署到服务器（推荐爱尔兰 AWS）

完整步骤见 `docs/live-runbook.md` §3：规格建议、NTP 对时、私钥传输
（绝不明文发消息）、启动脚本、监控。Docker 文件（Dockerfile +
docker-compose.yml，TimescaleDB 可选）已随包附带。

## 9. 已修复的重要坑（接手后可少踩一遍）

- CLOB market WS 同连接不可二次订阅帧：更新用 `operation: subscribe/unsubscribe` 增量（`feeds/market_ws.py`）
- 成交归属：post-only 单成交时是 MAKER，单号在 `maker_orders[]`（`execution/live.py`）
- 订单状态大小写：SDK 返回小写 `live`，引擎按大写匹配（曾导致到期撤单失效）
- 权益单位：SDK 余额是原始单位，需除 1e6（曾把 92 USDC 当成 9200 万）
- WS 流投递延迟 3-23s：入场决策改用 REST /books 轮询（`books.rest_poll_*`）
- 簿口新鲜度用"到达时刻"而非消息生成时间戳（流延迟不是簿口陈旧）
- 结算只认官方来源：PTB/final 都是 RTDS 才自结算，否则等 gamma 对账

## 10. 已知限制（v1）

- 交易所侧一律 GTC 挂单（CLOB 要求 GTD≥3 分钟）+ 本地 deadline 撤单；
  进程崩溃期间的挂单由启动时 cancel_all 兜底
- 风控状态（日亏/连亏/冷却）不跨重启持久化
- 费率仍硬编码 taker 1bp（未读 feeSchedule）
