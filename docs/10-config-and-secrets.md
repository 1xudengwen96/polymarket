# 10 — 配置与密钥管理

## 1. 铁律
- 所有敏感信息（私钥、API key/secret/passphrase、Chainlink 凭证、DB 密码）**只能**来自环境变量（或 `.env`，`.env` 在 `.gitignore` 内）。
- 代码仓库中只存在 `.env.example`（占位符）。
- 默认 `LIVE=false`；开启实盘需要**两个**条件同时满足：
  1. 环境变量 `PM5HFT_LIVE=true`
  2. `config/live.yaml` 中 `allow_live: true`（人工编辑确认，默认 false）
  → 任一缺失都只跑 Paper。
- 禁止自动扩大仓位（`auto_scale=false` 硬默认，扩大只能人工改配置）。

## 2. 环境变量（.env.example）

```bash
# ── 运行模式 ──
PM5HFT_MODE=paper                  # paper | live
PM5HFT_LIVE=false                  # 实盘总开关（必须显式 true）

# ── 实盘账户（仅 live 需要；Polygon L2 签名钱包）──
POLYMARKET_PRIVATE_KEY=            # 0x… 64 hex；绝不提交 git
POLYMARKET_FUNDER=                 # 可选：代理钱包的资金地址
POLYMARKET_SIGNATURE_TYPE=1
POLYMARKET_API_KEY=                # 可选：派生后缓存（SDK 可自动派生）
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=

# ── 可选：Chainlink Data Streams 直连凭证（更高精度 TWAP，可选）──
CHAINLINK_CLIENT_ID=
CHAINLINK_CLIENT_SECRET=

# ── 数据库 ──
PM5HFT_DB_URL=sqlite:///./data/pm5hft.db        # 生产: postgresql+psycopg://…

# ── 日志 ──
PM5HFT_LOG_LEVEL=INFO
```

## 3. 配置文件（config/，非敏感项）

```
config/
  strategy.yaml     # 04 §10 全部参数
  risk.yaml         # 06 §8 全部参数
  execution.yaml    # 延迟/重试/ttl/限速桶初始值
  probability.yaml  # 模型版本/特征开关/校准参数
  assets.yaml       # assets: [btc]; 每资产: slug 模板、twap 符号、binance 符号、窗口时长
  live.yaml         # allow_live: false（人工开启实盘前必须改这里）
```
用 `pydantic-settings` 加载：env 覆盖 yaml；启动时打印**脱敏后**的完整配置快照并写库。

## 4. 密钥泄露防护
- 日志脱敏器：任何 64-hex / api key 模式打码；
- 异常堆栈不包含请求体中的私钥字段（SDK 默认不打印，再加一层保险）；
- 生产用 Docker secrets / k8s secret 挂载 `.env`，文件权限 600。
