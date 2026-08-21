# 11 — 实施计划（Implementation Plan）

## 进度状态（2026-08-14 更新）

- ✅ **Phase 0 脚手架**：pyproject、Docker、config、.env.example、README、tests（30 个全过，ruff 全绿）
- ✅ **Phase 1 数据面（验收达成）**：
  - 窗口注册（Gamma slug 推导 ✓）、CLOB Market WS ✓、RTDS TWAP30/60 + crypto_prices ✓、Binance aggTrades ✓（`data-stream.binance.vision`，本网络 `stream.binance.com` 被封锁）
  - **PTB 捕获 ✓**：RTDS TWAP60 在窗口起点精确采样（obs 时间戳 == t_start）
  - **自结算 ✓**：窗口结束后 ~4.5s 内判定（实测 DOWN：PTB=63275.895 / final=63246.235）
  - **Gamma 对账 ✓**：跨进程 DB 对账路径验证一致（self=DOWN = gamma=DOWN，无 dispute）
  - 全链路持久化 ✓（markets/status/twap_samples/ticks/books/trades/settlements）
- 🔵 **Phase 2a 进行中**：概率（校准+Edge+占位模型）、风险（铁律/限额/熔断）、策略状态机（6 模块）+ 决策日志（每秒落库，执行未接）已完成并有单测；剩余：ExecutionEngine + PaperFillSim（2b）、回测（Phase 3）
- ✅ **Phase 2b 完成（验收达成）**：
  - ExecutionEngine（uuid7 幂等/确定性 salt/令牌桶限速/t_end-10s 拒单、t_end-5s 强制全撤/GTD 过期/FAK 剩余自动取消）
  - PaperFillSim（真实簿口驱动：队尾排队 maker 成交、部分成交、扫穿全成、post-only 穿越拒绝、taker 深度加权、250ms 延迟、FOK 深度不足过期）
  - LiveGateway 骨架（SDK SecureClient 门控，Phase 5 联调）
  - 决策→下单→成交→持仓→退出/对冲→结算 PnL（含费用）全链路实时验证：
    - 05:20 窗口：ENTER_UP@0.31 成交 → 模型反转退出@0.31 → 结算 pnl=-0.0019（= taker 费 0.00186，分毫不差）
    - 05:10 窗口：BUY 80@0.23 → 止损分两次 FAK 卖出（部分成交 17@0.17 + 63@0.08，FAK 剩余取消）→ pnl=-10.4708（含费）
    - 10 个窗口结算，全部 self=gamma 对账一致，0 争议
  - RTDS 静默停滞看门狗（收包超时强制重连）
  - 40 个测试全绿 + ruff 全绿
- ✅ **Phase 3.5 模型研究完成（docs/model-research-report.md）**：
  - 市场定价基准曲线：T-240 Brier 0.173 → T-60 0.031 → T-10 0.0008（边跑边定价）
  - **边际 edge 定位**：全模型在 T-60 桶 Brier 0.0226 vs 市场 0.0311（增益 27%）；T-240 无优势；T-10/T-20 市场已完美
  - **末段 10-20s 行为证伪**：末段收益系数 0.015 vs 早期 0.075，Brier 边际增益 0.000（TWAP60 摊薄效应定量证实）
  - 尾部 98-99.9¢ 实际胜率 99.24%（轻微低估，Tail Capture 理论空间 ~0.5-0.7%）；套利成交价近似含伪信号、真实测量需簿口历史
  - 策略时间守卫修正（entry_min_remaining_s 90→30，原设置恰好排除唯一有优势的 T-60 桶）；修正后严格回测 6 入场 -12.47 USDC——edge 存在但薄，需 T-60 专用 maker 策略变体 + 模型升级变现
  - 48 个测试全绿 + ruff 全绿
- ✅ **T-60 maker 变体验证完成（docs/profitability-verdict.md）——盈利性裁决：不能盈利**：
  - 五组配置（A 基线 / B T-60 限时 / C 公允价挂单 / C3 全放宽）5 天回测全部亏损（-12.5 ~ -54.5 USDC），全部触达风控小时亏损上限
  - 结构性亏损三因：逆向选择（2.5% 模型优势 < 2% 价差+费用）、末分钟执行风险（maker 价差捕获机制有效：平均盈利 +0.17≈2¢ 挂单差，但止损在末分钟跳空中以 1-5¢ 成交，平均亏损 -11 为盈利的 65 倍）、市场效率（T-10 Brier 0.0008）
  - 最有希望的转向：**15m/1h 市场**（T-240 桶市场定价最粗糙 Brier 0.173，错误定价空间大一个数量级）；引擎已支持仅改配置切换
- ✅ **Phase 3 回测引擎完成（验收达成，docs/backtest-report.md）**：
  - 数据管线：Binance aggTrades 日包（µs 时间戳，聚合 1s bars + 保留逐笔）→ Gamma 窗口元数据+结算标签 → data-api 逐笔成交序列（秒级时间戳陷阱已修）
  - **TWAP 标签重建（逐笔时间加权，最接近 Chainlink 方法论）**：5 天 1439/1440 可重建；129 个错配中 122 个集中在 |margin| <1.5 bps 的平局窗口；**过滤后错配率 0.72%**；±5/10s 敏感度分析一致率 89-93%
  - 事件驱动回放器：与实盘共用特征/概率/风控/策略/执行内核；回放时钟注入（修复墙钟污染 remaining_s/截止判定的 3 处 lookahead 级 bug）；簿口停滞守卫；有效 tick 规则（0.96+/0.04- → 0.001）
  - 模型管线：特征收集（5656 行）→ 逻辑回归训练（val acc 85.4%，Brier 0.085）→ 分桶校准（修复 pred<0.5 污染 0.99+ 桶的镜像分桶 bug，修复后桶胜率单调 0.53→1.00）
  - **诚实结论（5 天 1440 窗口）**：严格配置 6 窗口成交、20 笔成交、-15.07 USDC（-0.15%）、连续亏损熔断正确触发；占位模型无正期望 → 需要模型研究（分时间桶/路径特征/订单流），这正是回测阶段的科学价值
  - 48 个测试全绿 + ruff 全绿

## 阶段划分（每个阶段都有可运行产出与验收标准）

### Phase 0 — 项目脚手架（1 天）
- 仓库结构、pyproject（依赖：polymarket-client、websockets、sqlalchemy、alembic、pydantic-settings、structlog、numpy/pandas、lightgbm）、Dockerfile、docker-compose、.env.example、CI（ruff + mypy + pytest）。
- **验收**：`pytest` 通过；`python -m pm5hft --help` 可用。

### Phase 1 — 数据面（2-3 天）
- MarketReg（slug 推导 + Gamma 拉取 + 窗口注册）；
- FeedManager：CLOB Market WS 订阅（book/price_change/trades/tick_size）+ RTDS（TWAP 30/60 + crypto_prices）+ Binance aggTrades；
- TickRecorder（全部特征计算）；
- Persistence 全部 schema + 写入。
- **验收**：连续运行 30 分钟，捕获 ≥6 个窗口；PTB/结算采样正确（与 gamma 对账 100% 一致）；特征无 NaN 风暴。

### Phase 2 — 引擎内核（2-3 天）
- ProbabilityEngine（特征→模型→校准→Net Edge 管线；先用逻辑回归占位模型）；
- RiskEngine（限额/熔断/铁律）；
- StrategyEngine（状态机 + 6 模块）；
- ExecutionEngine + ExchangeGateway 抽象 + PaperGateway（PaperFillSim v1）。
- **验收**：Paper 模式跑 2 小时，决策日志完整，无违反铁律记录（自动断言检查）。

### Phase 3 — 回测（3-5 天）
- 历史数据管线（Gamma 元数据 + prices-history + Binance aggTrades）；
- TWAP 标签重建 + 与 gamma 对账校验（不一致率 < 2%）；
- BacktestFillSim + lookahead 防护；
- 指标报告 + 校准分桶报告。
- **验收**：≥1000 窗口报告产出；按 08 §5 门槛评估模型并调参。

### Phase 4 — Paper Trading 正式（3-7 天，与 Phase 3 可并行）
- 完整 paper 运行 ≥1000 笔交易；模拟器 vs 现实校验（09 §5）；
- TailCapture/TailHedge/Arb 模块实证（机会频率统计）；
- **验收**：08 §5 门槛 + paper 与回测结论同向。

### Phase 5 — 实盘小资金（人工确认后）
- LiveGateway（SDK 下单/撤单/User WS 对账 + 心跳 + 限速桶）；
- 密钥加载与 `allow_live` 双开关；
- 每笔 1~10 USDC，人工监控仪表盘（FastAPI 只读）；
- **验收**：连续稳定 + 人工确认后，才可人工调大资金（无自动扩仓）。

## 当前交付物（本阶段）
- `docs/00..11` 设计文档（本目录）。
- 待用户确认：技术栈（Python）、数据库（SQLite→Timescale）、数据源组合（RTDS + Binance WS + Gamma/CLOB），以及是否按本计划开始 Phase 0/1 编码。

## 目录结构（Phase 0 创建）
```
D:\DSH\
  docs\                     # 设计文档（本阶段产出）
  pm5hft\                   # Python 包
    main.py                 # 入口（paper/live）
    supervisor.py
    market\                 # MarketReg / 窗口时钟
    feeds\                  # market_ws, user_ws, rtds, binance
    features\               # 特征计算（三模式共用）
    probability\            # 模型 + 校准
    strategy\               # 状态机 + 6 模块
    risk\                   # 限额/熔断
    execution\              # 引擎 + gateway(live/paper)
    persistence\            # 表 + 仓储
    report\                 # 指标报告
    backtest\               # 回放/数据管线
  config\                   # yaml 配置
  tests\
  data\                     # sqlite/缓存（gitignore）
  scripts\                  # 数据抓取/维护脚本
  .env.example
  pyproject.toml  Dockerfile  docker-compose.yml  README.md
```
