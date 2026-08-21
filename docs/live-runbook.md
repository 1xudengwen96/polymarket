# 实盘上线手册（v1，小资金验证版）

> 目标：用最小资金验证"纸面成交率在实盘是否成立"。
> 铁律：**第一周每笔 5 USDC、只跑 BTC/ETH 尾部策略、实验模块强制关闭、人工盯盘。**
> 部署顺序：**先在本地 Windows 跑通 → 确认无问题 → 再部署爱尔兰 AWS。**

## 0. 实盘已实现的能力（v1）

- 限价单（post-only，交易所侧 GTC + 本地截止撤单 t_end-5s）
- FAK（限价保护市价单：BUY=max_price / SELL=min_price）
- 撤单（单笔 + 启动 cancel_all 兜底）
- 成交回写（SDK 用户流 UserTradeEvent → 账本，含费率、去重）
- 实盘权益 = USDC collateral 余额（风控限额基数）
- 实验模块在 live 模式强制关闭（mid/xrp_fade/arb）

## 1. 账户准备（一次性，App 完成）

1. polymarket.com 注册 → 用**你自己的 EOA 钱包**（MetaMask 等）连接
2. 充值 USDC（Polygon）——资金进入**存款钱包**（SDK 自动派生/查找）
3. 导出该 EOA 的私钥 → 填入 `POLYMARKET_PRIVATE_KEY`
4. 预检里"余额 > 0"通过即可

## 2. 本地先行（Windows，明早第一步）

```powershell
# ① 时钟确认
w32tm /query /status     # Source 应为已同步的 NTP 源

# ② 电源设置：关闭休眠（live 会话期间电脑不能睡）
powercfg /change standby-timeout-ac 0

# ③ 环境变量（每个新 PowerShell 窗口都要设；建议写进启动脚本）
$env:PM5HFT_MODE='live'
$env:PM5HFT_LIVE='true'
$env:POLYMARKET_PRIVATE_KEY='<EOA 私钥>'
$env:PM5HFT_DB_URL='sqlite+aiosqlite:///./data/pm5hft-live.db'   # 独立 live 账本
$env:PYTHONIOENCODING='utf-8'

# ④ 开关
config/live.yaml → allow_live: true

# ⑤ 预检（只读，不下单不撤单）
python scripts/live_preflight.py

# ⑥ 启动（单独窗口，日志落盘）
python -m pm5hft.main --log-level INFO 2>&1 | Tee-Object logs\live-$(Get-Date -Format yyyyMMdd-HHmm).log

# ⑦ 面板（单独端口，与纸面面板 8090 分开）
python -m pm5hft.dashboard --port 8091 --db data/pm5hft-live.db
```

注意：
- **本机纸面盘继续跑不受影响**（它的进程 env 不变、DB 不变）
- 启动日志应出现：`LIVE mode: experiments force-disabled`、`live client created`、
  `startup safety: cancelled all open orders`、`live equity loaded ...`
- 若公司/家庭网络有代理或防火墙，SDK 的 REST（clob.polymarket.com）走 HTTPS，
  预检的余额读取一步就能暴露问题

## 3. 部署爱尔兰 AWS（本地跑通后）

```bash
# ① 代码上传（pm5hft/、config/、artifacts/、pyproject.toml、scripts/）
# ② 依赖
pip install -e .
# ③ 环境变量（.env 或 systemd）
PM5HFT_MODE=live
PM5HFT_LIVE=true
POLYMARKET_PRIVATE_KEY=<EOA 私钥，勿提交 git>
POLYMARKET_FUNDER=<可选：存款钱包地址；留空=签名者的存款钱包>
PM5HFT_DB_URL=sqlite+aiosqlite:///./data/pm5hft-live.db
# ④ config/live.yaml → allow_live: true
# ⑤ timedatectl 确认 synchronized: yes
# ⑥ python scripts/live_preflight.py
```

## 4. 92 USDC 资金档说明（明早默认）

- **每笔 5 USDC** = 市场最小单量（5 股 × ~0.98）——仓位公式已加下限
  （92 × 0.5% = 0.46 会被抬到 5），实盘自动生效，无需改配置
- **实盘专属风控档**（`config/live_risk.yaml`，mode=live 自动加载）：

| 限额 | 值 | 含义 |
|---|---|---|
| 未对冲敞口 | 12%（≈11 USDC） | 最多 2 笔并发持仓（策略正常形态） |
| 日亏熔断 | 12%（≈11 USDC） | **2 只天鹅熔断当日**（1 只 -4.9 可继续） |
| 小时冷却 | 6%（≈5.5） | 1 只天鹅冷却 12 窗口 |
| 回撤 kill | 20%（≈18.4） | 3-4 只天鹅后终止 |
| 市场占用 | 20%（≈18.4） | 2 笔并发不触发 |

- **现实预期**：92 USDC、无天鹅的理想日 ≈ +0.5~1.5；一只天鹅 ≈ -4.9。
  这个资金量的目标是**验证成交率**，不是盈利规模。

## 5. 盯盘要点（第一小时必看）

| 指标 | 正常 | 异常动作 |
|---|---|---|
| 决策流 | 每秒 NOOP/TAIL_CAPTURE | 无决策 → 查 feed 连接 |
| 挂单状态 | LIVE → FILLED / EXPIRED / CANCELLED | 大量 REJECTED → 查 tick/post-only |
| 成交 src | `live` | 出现 `paper_sim` → 模式错了 |
| 权益 | 与交易所余额一致 | 偏差 → 立即停 |
| dispute | 全 ✓ | ⚠ 出现 → 记下市场号 |

## 6. 紧急停止

```powershell
# Windows
Get-Process python | Stop-Process     # 注意会连纸面盘一起停
# 更精确：记下 live 进程的 PID 后 Stop-Process -Id <pid>
# 停后启动一次即自动 cancel_all 清残留；不放心可在 App 手动撤单
```

## 7. v1 已知限制（诚实清单）

1. **GTD 改 GTC + 本地撤单**：进程在窗口最后 5 秒内崩溃可能留下挂单跨窗口
   （下次启动 cancel_all 兜底；首周人工盯盘覆盖此风险）
2. **风控状态重启重置**：kill/日亏/连亏在重启后归零（权益已从余额恢复）。
   第一周人工盯盘；后续版本补状态恢复
3. **无测试网**：第一笔实盘订单就是第一次真实验证 → 小注 + 盯盘
4. 数据中心 IP（AWS）的交易所策略未知 → 本地先行正好给出对比基线

## 8. 首周目标（数据）

- 实盘 maker 成交率 vs 纸面（BTC 98% / ETH 61%）
- 末段冲刺窗口的队列位置损耗
- 实盘费率（feeSchedule）与 1bp 假设的偏差
- 本地 vs AWS 的网络表现对比（延迟/断流频率）
