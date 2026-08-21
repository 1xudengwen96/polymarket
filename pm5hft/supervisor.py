"""Supervisor：任务编排、窗口边界、看门狗、优雅停机。"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import aiohttp

from .clock import AssetWindow, beijing_date, in_trading_window, now_ms, now_s
from .config import Config, resolve_run_path
from .db import close_db, create_schema, init_db
from .execution import ExecutionEngine, LiveGateway, OrderIntent, PaperGateway
from .execution.engine import FillEvent
from .features import FeatureStore
from .feeds import BinanceFeed, MarketWsFeed, RtdsFeed
from .logging_setup import get_logger
from .market_registry import MarketRegistry
from .persistence import Repo
from .probability.calibration import Calibrator
from .probability.edge import compute_edge
from .probability.model import BaselineModel, load_artifact
from .risk import RiskEngine
from .strategy import StrategyEngine
from .twap import TwapService, rebuild_ptb_from_buffer

PREREGISTER_LEAD_S = 75  # 提前注册下一窗口（需早于 PTB 捕获）
REFRESH_INTERVAL_S = 30  # 当前窗口 gamma 状态刷新
RECONCILE_AFTER_END_S = 45  # 窗口结束后开始对账
REGISTER_RETRY_S = 10  # 窗口注册失败后的重试节流（gamma 断线自愈）


class Supervisor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.log = get_logger("supervisor")
        # 实盘安全铁律：实验模块一律不进实盘（独立账本逻辑仅限纸面）
        # ——必须在策略引擎构造之前禁用，否则引擎会读到旧配置
        if config.settings.mode == "live":
            config.strategy["mid_capture"]["enabled"] = False
            config.strategy["xrp_fade"]["enabled"] = False
            config.strategy["arb"]["enabled"] = False
            # 实盘出场用纯 taker：纸面实测 124 笔出场 120 笔走 FAK 兜底，
            # maker 出场挂单在真实延迟下几乎必然 post-only 被拒（白费一次下单+拒绝噪声）
            config.strategy["exit"]["exit_mode"] = "taker"
            self.log.info("LIVE mode: experiments force-disabled (mid/xrp_fade/arb), exit_mode=taker")
        self.repo = Repo()
        self.registry = MarketRegistry(config, self.repo)
        self.features = FeatureStore(config)
        self.twap = TwapService(config, self.repo, binance_twap=self._rebuild_ptb,
                                on_gamma_settle=self._settle_market_by_gamma)

        # Phase 2 引擎
        self.risk = RiskEngine(config, mode=config.settings.mode)
        self.calibrator = Calibrator(
            min_n=int(config.p("min_bucket_n", 200)),
            cold_start_shrink=float(config.p("cold_start_shrink", 0.85)),
        )
        # 加载训练工件（模型+校准桶）；缺失时兜底 BaselineModel + 冷启动校准
        model = BaselineModel()
        artifact_path = config.p("model_artifact", None)
        if artifact_path:
            artifact_path = resolve_run_path(artifact_path)  # frozen exe 时相对 exe 目录
        loaded = load_artifact(artifact_path)
        if loaded is not None:
            model, cal_rows = loaded
            self.calibrator.load_rows(cal_rows)
            self.log.info("model artifact loaded", path=artifact_path, cal_buckets=len(cal_rows))
        else:
            self.log.warning("model artifact missing/unreadable; using BaselineModel (tail capture disabled)",
                             path=artifact_path)
        self.strategy = StrategyEngine(config, self.risk, model, self.calibrator)
        self._settled_accounted: set[int] = set()

        # Phase 2b 执行层（paper 默认；live 需双开关）
        if config.settings.mode == "live":
            self.gateway = LiveGateway(config)
        else:
            self.gateway = PaperGateway(config, self.repo, self.features)
        self.execution = ExecutionEngine(
            config, self.repo, self.gateway,
            on_fill=self._on_fill_event,
            deadline_for=self._deadline_for,
            on_order_closed=self._on_order_closed,
        )
        if isinstance(self.gateway, PaperGateway):
            self.gateway.fill_handler = self.execution.handle_fill
        elif isinstance(self.gateway, LiveGateway):
            self.gateway.fill_handler = self.execution.handle_fill

        binance_symbols = [a.binance_symbol for a in config.enabled_assets().values()]
        twap_symbols = sorted({a.twap_symbol for a in config.enabled_assets().values()})
        self.rtds = RtdsFeed(
            binance_symbols=binance_symbols,
            twap_symbols=twap_symbols,
            on_crypto_price=self._on_crypto_price,
            on_twap=self._on_twap,
        )
        self.market_ws = MarketWsFeed(on_event=self._on_market_event)
        self.binance = BinanceFeed(symbols=binance_symbols, on_trade=self._on_trade)

        self._last_window: dict[str, int] = {}
        self._last_register_attempt: dict[tuple[str, int], float] = {}  # (asset, t_start) -> 上次注册尝试时刻
        self._last_persisted_bar: dict[str, int] = {}
        self._last_book_persist: dict[str, tuple[str, int]] = {}  # token -> (hash, ts)
        self._last_resubscribe = 0.0  # 订阅保险计时（每 60s 重发一次）
        self._last_market_ws_reconnect = 0.0  # 簿口新鲜度自愈节流
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_heartbeat = time.monotonic()
        self._runtime_enabled = True
        self._runtime_notional = Decimal(str(config.s("tail_capture.fixed_order_notional", 5)))
        self._tail_entry_price = Decimal(str(config.s("tail_capture.entry_price_min", 0.98)))
        self._tail_exit_price = Decimal(str(config.s("tail_capture.exit_price", 0)))
        self._entry_delay_enabled = bool(config.s("tail_capture.entry_delay_enabled", False))
        self._entry_delay_min = int(config.s("tail_capture.entry_delay_minutes", 3))
        self._last_risk_state: str | None = None  # 发布给 Dashboard 显示的风控状态
        # 提速缓存：runtime_settings 1s 读一次（面板改动最多 1s 生效）；
        # 成交回调免 DB 读订单 meta（同订单部分成交多次时省读+解析）
        self._rt_cache: dict[str, str] | None = None
        self._rt_cache_ts = 0.0
        self._fill_cache: dict[str, tuple[str, dict]] = {}  # client_order_id -> (token_id, meta)
        self._last_decision_log_ms = 0  # decision_log 每 tick 写 → 降频到 1/s
        # 交易日程状态（每日止盈目标 + 北京时间交易时段；休息原因写回 DB 供 Dashboard 展示）
        self._bt_day: str | None = None        # 当前北京日期（止盈计数按北京日重置）
        self._bt_session_pnl = Decimal("0")    # 当日（北京日）已实现利润累计（随启动清零）
        self._profit_target_hit = False        # 止盈目标已达成 → 当日休息中
        self._rest_reason = "none"             # none | manual | profit_target | trading_hours

    # ── 回调 ─────────────────────────────────────────────────
    async def _on_crypto_price(self, symbol: str, ts_ms: int, value) -> None:
        await self.features.on_crypto_price(symbol, ts_ms, value)

    async def _on_twap(self, symbol: str, window_s: int, value, full_e18: str, obs_ts_ms: int) -> None:
        await self.twap.on_twap(symbol, window_s, value, full_e18, obs_ts_ms)

    async def _on_trade(self, symbol: str, ts_ms: int, price: float, qty: float, is_buyer_maker: bool) -> None:
        await self.features.on_trade(symbol, ts_ms, price, qty, is_buyer_maker)

    async def _on_market_event(self, event_type: str, msg: dict[str, Any]) -> None:
        try:
            if event_type == "book":
                await self.features.on_book_event(msg)
                await self._maybe_persist_book(msg)
            elif event_type == "price_change":
                await self.features.on_price_change(msg)
            elif event_type == "last_trade_price":
                await self.features.on_last_trade(msg)
                await self._maybe_persist_trade(msg)
                # paper 撮合：真实成交流驱动 resting 单成交
                if isinstance(self.gateway, PaperGateway):
                    token_id = msg.get("asset_id") or ""
                    try:
                        price = Decimal(str(msg.get("price") or "0"))
                        size = Decimal(str(msg.get("size") or "0"))
                        side = str(msg.get("side") or "")
                        if token_id and price > 0 and size > 0:
                            await self.gateway.on_trade(token_id, price, size, side, now_ms())
                    except Exception:
                        self.log.exception("paper matching failed", token=token_id)
            elif event_type == "tick_size_change":
                await self.features.on_tick_size_change(msg)
            elif event_type == "best_bid_ask":
                pass
            elif event_type == "market_resolved":
                self.log.info("market_resolved event", msg={k: msg.get(k) for k in ("market", "asset_id")})
            elif event_type == "new_market":
                q = str(msg.get("question") or "")
                if "Up or Down" in q or "updown" in q.lower():
                    self.log.info("new updown market", question=q[:100])
        except Exception:
            self.log.exception("market event handler failed", event_type=event_type)

    async def _maybe_persist_book(self, msg: dict[str, Any]) -> None:
        token_id = msg.get("asset_id") or ""
        if not token_id:
            return
        hash_ = msg.get("hash")
        ts = int(msg.get("timestamp") or 0) or now_ms()
        last = self._last_book_persist.get(token_id)
        if last and last[0] == hash_ and ts - last[1] < 2000:
            return
        book = self.features.books.get(token_id)
        if book is None:
            return
        bids, asks = book.top(10)
        await self.repo.insert_book_snapshot(
            token_id=token_id,
            book_hash=hash_,
            best_bid=format(book.best_bid, ".2f") if book.best_bid is not None else None,
            best_ask=format(book.best_ask, ".2f") if book.best_ask is not None else None,
            bid10=json.dumps([[f"{p:.2f}", f"{s:.2f}"] for p, s in bids]),
            ask10=json.dumps([[f"{p:.2f}", f"{s:.2f}"] for p, s in asks]),
            tick_size=book.tick_size,
        )
        self._last_book_persist[token_id] = (hash_, ts)

    async def _maybe_persist_trade(self, msg: dict[str, Any]) -> None:
        token_id = msg.get("asset_id") or ""
        rec = self._record_for_token(token_id)
        await self.repo.insert_trade(
            market_id=rec.market_id if rec else None,
            token_id=token_id,
            side=str(msg.get("side") or ""),
            price=str(msg.get("price") or ""),
            size=str(msg.get("size") or ""),
        )

    def _record_for_token(self, token_id: str):
        for rec in self.registry._markets.values():
            if token_id in (rec.token_up, rec.token_down):
                return rec
        return None

    def _deadline_for(self, market_id: int) -> int | None:
        for rec in self.registry._markets.values():
            if rec.market_id == market_id:
                return rec.t_end * 1000
        return None

    # ── 成交回写：策略/风控状态 ───────────────────────────────
    def _on_order_closed(self, client_order_id: str, market_id: int, state: str) -> None:
        """挂单过期/撤单：若为未成交的入场单 → 允许策略重新评估。"""
        self.strategy.on_order_expired(market_id)

    async def _on_fill_event(self, fill: FillEvent) -> None:
        # 提速：同订单多次成交（部分成交）免重复 DB 读订单 + JSON 解析
        cached = self._fill_cache.get(fill.order_id)
        if cached is not None:
            token_id, meta = cached
        else:
            # 权威 token = 订单记录的 token_id（实盘 2026-08-21 事故根因：live 网关
            # UserTradeEvent 的 payload.token_id 与订单 token 不一致，直接用它会把 UP
            # 成交标成 DOWN → 策略内存账本方向反转 → 对冲买错边、出场卖错边/拒单）
            order = await self.repo.get_order(fill.order_id)
            if order is None:
                self.log.warning("fill for unknown order", order=fill.order_id)
                return
            token_id = order.token_id or fill.token_id
            meta: dict[str, Any] = {}
            if order.meta:
                try:
                    meta = json.loads(order.meta)
                except json.JSONDecodeError:
                    meta = {}
            if len(self._fill_cache) > 2000:
                self._fill_cache.clear()
            self._fill_cache[fill.order_id] = (token_id, meta)
        rec = self._record_for_token(token_id)
        if rec is None:
            return
        token_side = "UP" if token_id == rec.token_up else "DOWN"
        if meta.get("module") == "entry":
            pos = self.strategy.positions.get(rec.market_id)
            if pos is not None:
                held = pos.up_qty if token_side == "UP" else pos.down_qty
                meta["first_fill"] = held <= 0
        self.strategy.on_fill(rec.market_id, token_side, fill.qty, fill.price, fill.side, meta)
        self.log.info("FILL", market=rec.market_id, side=fill.side, token=token_side,
                      price=str(fill.price), qty=str(fill.qty), src=fill.src)

    # ── 窗口循环 ─────────────────────────────────────────────
    async def _window_loop(self) -> None:
        while not self._stop.is_set():
            self._last_heartbeat = time.monotonic()
            try:
                await self._tick_window()
            except Exception:
                self.log.exception("window tick failed")
            await asyncio.sleep(1.0)

    async def _tick_window(self) -> None:
        now = now_s()
        for name, acfg in self.config.enabled_assets().items():
            t_start = (now // acfg.duration_s) * acfg.duration_s
            if self._last_window.get(name) == t_start:
                continue
            key = (name, t_start)
            if time.monotonic() - self._last_register_attempt.get(key, 0.0) < REGISTER_RETRY_S:
                continue  # 上次注册失败 → 节流重试（防 gamma 恢复前过度重试）
            self._last_register_attempt[key] = time.monotonic()
            win = AssetWindow(asset=name, tf_label=acfg.tf_label, t_start=t_start, duration_s=acfg.duration_s)
            # 仅在注册成功时标记窗口已处理；失败则每 REGISTER_RETRY_S 重试，
            # 否则 gamma 在窗口边界断线会导致整窗口无市场记录、面板停摆
            if await self._on_new_window(name, acfg, win):
                self._last_window[name] = t_start

    async def _on_new_window(self, name: str, acfg, win: AssetWindow) -> bool:
        rec = self.registry.get(name, win.t_start)
        if rec is None:
            rec = await self.registry.register_window(name, acfg, win)
        if rec is None:
            self.log.error("window not registered", asset=name, t_start=win.t_start)
            return False
        lookback = rec.twap_lookback_s or acfg.twap_lookback_default
        if rec.twap_lookback_s is None:
            self.log.warning("twap_lookback missing, using default", asset=name, default=lookback)
        self.twap.on_window_start(acfg, win, lookback)
        self.features.tick_buffers[name].reset_window(win.t_start)
        self.features.set_active_tokens(rec.token_up, rec.token_down)
        # 更新 WS 订阅（当前 + 已预注册的下一窗口）
        self.market_ws.set_asset_ids(self.registry.subscription_tokens())
        await self.market_ws.resubscribe()
        self.log.info("window started", asset=name, slug=win.slug, accepting=rec.accepting_orders)
        await self.repo.add_market_status(rec.market_id, "trading", accepting_orders=rec.accepting_orders)
        return True

    # ── 预注册 / 刷新 / 对账 ──────────────────────────────────
    async def _housekeeping_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._housekeeping()
            except Exception:
                self.log.exception("housekeeping failed")
            await asyncio.sleep(10.0)

    async def _housekeeping(self) -> None:
        now = now_s()
        # 清理过期窗口（订阅列表保持精简：防交易所订阅帧截断尾部 token）
        pruned = self.registry.prune(now - 600)
        if pruned:
            self.log.info("registry pruned", n=pruned)
        # 订阅保险：每 60s 重发一次（diff 增量；无变化时为空操作，
        # 防单次 subscribe 更新被服务端丢弃 → 新窗口簿口空白）
        if time.monotonic() - self._last_resubscribe > 60:
            self.market_ws.set_asset_ids(self.registry.subscription_tokens())
            await self.market_ws.resubscribe()
            self._last_resubscribe = time.monotonic()
        # 簿口新鲜度自愈：当前窗口簿口静默 >60s（WS 活着但订阅失效的失效模式）
        # → 强制 market_ws 重连（重连用最新订阅集发初始帧）
        if time.monotonic() - self._last_market_ws_reconnect > 60:
            for name, acfg in self.config.enabled_assets().items():
                rec = self.registry.get(name, (now // acfg.duration_s) * acfg.duration_s)
                if rec is None:
                    continue
                age = self.features.book_age_ms(rec.token_up)
                if age is None or age > 60_000:
                    self.log.warning(
                        "book stale, forcing market_ws reconnect",
                        asset=name, age_ms=age,
                    )
                    self._last_market_ws_reconnect = time.monotonic()
                    await self.market_ws.force_reconnect()
                    break
        for name, acfg in self.config.enabled_assets().items():
            t_start = (now // acfg.duration_s) * acfg.duration_s
            # 预注册下一窗口（在 PTB 捕获前拿到 lookback）
            next_start = t_start + acfg.duration_s
            if next_start - now <= PREREGISTER_LEAD_S:
                next_win = AssetWindow(asset=name, tf_label=acfg.tf_label, t_start=next_start, duration_s=acfg.duration_s)
                if self.registry.get(name, next_start) is None:
                    await self.registry.register_window(name, acfg, next_win)
                    self.market_ws.set_asset_ids(self.registry.subscription_tokens())
                    await self.market_ws.resubscribe()
            # 刷新当前窗口状态（间隔节流）
            cur = self.registry.get(name, t_start)
            if cur is not None and now_ms() - cur.last_refresh_ms > REFRESH_INTERVAL_S * 1000:
                await self.registry.refresh(name, acfg, cur)
            # 跨进程兜底：DB 中未对账的结算 → 注册对应市场以便对账
            for s in await self.repo.get_unreconciled_settlements(max_age_s=3600):
                if any(r.market_id == s.market_id for r in self.registry._markets.values()):
                    continue
                m = await self.repo.get_market_by_id(s.market_id)
                if m is None or m.asset != name:
                    continue
                win = AssetWindow(asset=name, tf_label=acfg.tf_label,
                                  t_start=m.t_start, duration_s=m.duration_s)
                await self.registry.register_window(name, acfg, win)
            # 对账：遍历已结束窗口。上界放宽到 1 小时（与未对账兜底重注册的
            # max_age_s=3600 对齐）：gamma 官方结果可能晚于窗口结束 15 分钟才出，
            # 否则结算会被永久搁置（实盘案例：08:30 结束的窗口 08:56 才 closed）。
            for rec in self.registry.windows_for(name):
                if rec.t_end + RECONCILE_AFTER_END_S <= now <= rec.t_end + 3600:
                    wt = self.twap.find(name, rec.t_start)
                    if wt is not None and wt.reconciled:
                        continue
                    db_settle = await self.repo.get_settlement(rec.market_id)
                    if db_settle is not None and db_settle.reconciled:
                        continue
                    await self.registry.refresh(name, acfg, rec)
                    await self.twap.reconcile_gamma(name, rec)

    # ── 周期任务 ─────────────────────────────────────────────
    async def _twap_timeout_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.twap.check_timeouts()
            except Exception:
                self.log.exception("twap timeout check failed")
            await asyncio.sleep(1.0)

    async def _tick_persist_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            try:
                self.features.roll_bars()
                for name, buf in self.features.tick_buffers.items():
                    if not buf.bars:
                        continue
                    last_ts = self._last_persisted_bar.get(name)
                    # 持久化所有完成且未写过的 bar（旧逻辑只写最后一条，忙时会漏秒）
                    for bar in buf.bars:
                        if last_ts is not None and bar.ts_ms <= last_ts:
                            continue
                        self._last_persisted_bar[name] = bar.ts_ms
                        await self.repo.insert_tick(
                            asset=name,
                            ts_ms=bar.ts_ms,
                            price=format(bar.c, ".6f"),
                            vol_1s=format(bar.vol, ".6f"),
                            agg_buy_1s=format(bar.agg_buy, ".6f"),
                            agg_sell_1s=format(bar.agg_sell, ".6f"),
                            n_trades_1s=bar.n,
                        )
            except Exception:
                self.log.exception("tick persist failed")

    async def _status_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30.0)
            try:
                snap = self.risk.snapshot()
                await self.repo.insert_equity_snapshot(
                    equity=snap["equity"],
                    daily=snap["daily_pnl"],
                    hourly=snap["hourly_pnl"],
                    drawdown=str(self.risk.drawdown()),
                    mode=self.config.settings.mode,  # paper/live 如实落库（曾硬编码 paper）
                )
                self.log.info(
                    "status",
                    twap=self.twap.status(),
                    risk=snap,
                    rest_reason=self._rest_reason,
                    session_pnl=str(self._bt_session_pnl),
                    feeds={
                        "rtds": self.rtds.is_ready,
                        "market_ws": self.market_ws.is_ready,
                        "binance": self.binance.is_ready,
                    },
                    flushed=self.repo._flushed,
                    dropped=self.repo._dropped,
                    execution=(
                        {"resting": self.gateway.resting_summary()}
                        if isinstance(self.gateway, PaperGateway)
                        else {"mode": "live"}
                    ),
                )
            except Exception:
                self.log.exception("status failed")

    # ── 策略决策循环（Phase 2b：决策 → 执行 → 成交 → 结算记账）─
    async def _strategy_loop(self) -> None:
        interval = float(self.config.s("runtime.strategy_tick_interval_s", 0.5))
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            try:
                await self._strategy_tick()
            except Exception:
                self.log.exception("strategy tick failed")

    async def _strategy_tick(self) -> None:
        await self._refresh_runtime_controls()
        for name, acfg in self.config.enabled_assets().items():
            win = self.registry.current_window(acfg)
            rec = self.registry.get(name, win.t_start)
            wt = self.twap.current(name)
            if rec is None or wt is None or wt.ptb is None or wt.self_result is not None:
                continue
            f = self.features.features(name, rec, wt)
            raw_p = self.strategy.model.predict(f)
            cal = self.calibrator.calibrate(raw_p)
            up_bid, up_ask = f.get("up_bid"), f.get("up_ask")
            market_p = (float(up_bid) + float(up_ask)) / 2.0 if up_bid is not None and up_ask is not None else None
            edge = None
            if market_p is not None:
                edge = compute_edge(
                    cal.cal_prob, market_p, self.strategy._taker_fee(rec),
                    float(f.get("remaining_s") or 60.0),
                    float(self.config.p("model_err_cold_start", 0.03)),
                    float(self.config.p("risk_buffer_global", 0.005)),
                )
            d = self.strategy.decide(f, rec, wt)
            pos = self.strategy.positions.get(rec.market_id)
            # 提速：decision_log 直接写库，从每 tick 降频到 1/s（复盘粒度足够）
            if now_ms() - self._last_decision_log_ms >= 1000:
                self._last_decision_log_ms = now_ms()
                await self.repo.add_decision(rec, wt, f, d, pos, cal.cal_prob, market_p, edge)
            if d.action != "NOOP":
                self.log.info("decision", action=d.action, side=d.token_side,
                              price=d.price, qty=d.qty, reason=d.reason)
                await self._dispatch_decision(rec, d)

        # 窗口自结算 → PnL 结算 + 策略/风险记账（每窗口一次）
        for rec in list(self.registry._markets.values()):
            if rec.market_id in self._settled_accounted:
                continue
            wt = self.twap.find(rec.asset, rec.t_start)
            if wt is not None and wt.self_result is not None and wt.settled_at_ms:
                self._settled_accounted.add(rec.market_id)
                # 撤掉该市场残留挂单（正常流程应在 t_end-5s 已全撤）
                await self.execution.cancel_all_market(rec.market_id)
                if wt.self_result in ("UP", "DOWN"):
                    pnl = Decimal(await self.repo.settle_positions(rec.market_id, wt.self_result))
                    self._bt_session_pnl += pnl  # 止盈目标计数（北京日累计）
                    self.strategy.on_settlement(rec.market_id, wt.self_result, pnl)
                    self.log.info("window settled", market=rec.market_id, result=wt.self_result,
                                  pnl=str(pnl), equity=str(self.risk.equity))
                else:
                    open_pos = await self.repo.get_open_positions(rec.market_id)
                    if open_pos:
                        self.log.error("settlement UNKNOWN with open positions", market=rec.market_id,
                                       positions=[(p.token_id, p.qty) for p in open_pos])

    async def _refresh_runtime_controls(self) -> None:
        # 提速：runtime_settings 最多 1s 读一次（面板设置变化 ≤1s 生效）
        now_mono = time.monotonic()
        if self._rt_cache is None or now_mono - self._rt_cache_ts >= 1.0:
            self._rt_cache = await self.repo.get_runtime_settings()
            self._rt_cache_ts = now_mono
        values = self._rt_cache
        enabled = values.get("auto_trading_enabled", "true").lower() == "true"
        try:
            notional = Decimal(values.get("fixed_order_notional", "5"))
        except Exception:  # noqa: BLE001
            notional = Decimal("5")
        notional = max(Decimal("5"), min(Decimal("100"), notional))
        # 尾盘入场价（Dashboard 可覆盖；0.50-0.999）
        try:
            entry_price = Decimal(str(values.get(
                "tail_entry_price", self.config.s("tail_capture.entry_price_min", 0.98))))
        except Exception:  # noqa: BLE001
            entry_price = Decimal("0.98")
        entry_price = max(Decimal("0.50"), min(Decimal("0.999"), entry_price))
        # 尾盘出场价（Dashboard 可覆盖；0 = 关闭/持有到结算，上限 0.999）
        try:
            exit_price = Decimal(str(values.get(
                "tail_exit_price", self.config.s("tail_capture.exit_price", 0))))
        except Exception:  # noqa: BLE001
            exit_price = Decimal("0")
        exit_price = max(Decimal("0"), min(Decimal("0.999"), exit_price))
        # 延迟进场（Dashboard 开关 + 第几分钟）
        try:
            delay_on = values.get(
                "entry_delay_enabled",
                str(bool(self.config.s("tail_capture.entry_delay_enabled", False))),
            ).lower() == "true"
        except Exception:  # noqa: BLE001
            delay_on = False
        try:
            delay_min = int(values.get(
                "entry_delay_minutes", self.config.s("tail_capture.entry_delay_minutes", 3)))
        except (TypeError, ValueError):
            delay_min = 3
        delay_min = max(0, min(4, delay_min))
        # 熔断解锁请求（Dashboard「解锁熔断」按钮）：一次性，处理完置回 0
        if values.get("reset_kill_request", "0").lower() in ("1", "true"):
            self.risk.reset_kill()
            self.log.warning("KILL switch reset via dashboard unlock button")
            try:
                await self.repo.set_runtime_setting("reset_kill_request", "0")
            except Exception:  # noqa: BLE001
                self.log.exception("failed to clear reset_kill_request")
        # 发布风控状态给 Dashboard（变化时写一次，供「解锁熔断」按钮显示）
        risk_state = self.risk.state.value
        if risk_state != self._last_risk_state:
            self._last_risk_state = risk_state
            try:
                await self.repo.set_runtime_setting("risk_state", risk_state)
            except Exception:  # noqa: BLE001
                self.log.exception("failed to persist risk_state")

        # ── 交易日程一：每日止盈目标（北京日计数，随启动清零） ──────────
        # 北京 0 点换日：重置计数并解除当日止盈休息（与风控的 UTC 日亏限额互不影响）
        today_bt = beijing_date()
        if self._bt_day is None or today_bt != self._bt_day:
            if self._bt_day is not None and self._profit_target_hit:
                self.log.info("Beijing day rolled over; profit target counter reset, trading may resume")
            self._bt_day = today_bt
            self._bt_session_pnl = Decimal("0")
            self._profit_target_hit = False
        try:
            target = Decimal(str(values.get(
                "daily_profit_target", self.config.s("schedule.daily_profit_target_usdt", 0) or 0)))
        except Exception:  # noqa: BLE001
            target = Decimal("0")
        target = max(Decimal("0"), target)
        target_hit = target > 0 and self._bt_session_pnl >= target

        # ── 交易日程二：北京时间交易时段（小时粒度，支持跨夜） ───────────
        try:
            hours_on = values.get(
                "trading_hours_enabled",
                str(bool(self.config.s("schedule.trading_hours_enabled", False))),
            ).lower() == "true"
        except Exception:  # noqa: BLE001
            hours_on = False
        try:
            start_h = int(values.get("trading_hours_start_bt",
                                     self.config.s("schedule.trading_hours_start_bt", 9)))
            end_h = int(values.get("trading_hours_end_bt",
                                   self.config.s("schedule.trading_hours_end_bt", 21)))
        except (TypeError, ValueError):
            start_h, end_h = 9, 21
        start_h %= 24
        end_h %= 24
        in_window = (not hours_on) or in_trading_window(None, start_h, end_h)

        # 休息原因优先级：手动关闭 > 止盈达成 > 时段外
        if not enabled:
            rest_reason = "manual"
        elif target_hit:
            rest_reason = "profit_target"
        elif not in_window:
            rest_reason = "trading_hours"
        else:
            rest_reason = "none"
        effective = enabled and not target_hit and in_window

        changed = (effective != self._runtime_enabled or notional != self._runtime_notional
                   or rest_reason != self._rest_reason or entry_price != self._tail_entry_price
                   or exit_price != self._tail_exit_price
                   or delay_on != self._entry_delay_enabled or delay_min != self._entry_delay_min)
        was_enabled = self._runtime_enabled
        self._runtime_enabled = effective
        self._runtime_notional = notional
        self._tail_entry_price = entry_price
        self._tail_exit_price = exit_price
        self._entry_delay_enabled = delay_on
        self._entry_delay_min = delay_min
        self._profit_target_hit = target_hit
        self.strategy.set_runtime_controls(effective, notional, entry_price, exit_price,
                                           delay_on, delay_min)
        if rest_reason != self._rest_reason:
            self._rest_reason = rest_reason
            try:
                await self.repo.set_runtime_setting("rest_reason", rest_reason)
            except Exception:  # noqa: BLE001
                self.log.exception("failed to persist rest_reason")
        if changed:
            self.log.info("runtime controls updated", auto_trading=effective,
                          fixed_order_notional=str(notional), rest_reason=rest_reason,
                          profit_target=str(target), session_pnl=str(self._bt_session_pnl),
                          tail_entry_price=str(entry_price), tail_exit_price=str(exit_price),
                          entry_delay=[delay_on, delay_min],
                          trading_hours=[start_h, end_h] if hours_on else None)
        if was_enabled and not effective:
            entry_modules = {"entry", "tail_capture", "xrp_fade", "mid_capture", "arb"}
            cancelled = 0
            for order in await self.repo.get_all_open_orders():
                try:
                    module = (json.loads(order.meta or "{}") or {}).get("module")
                except (json.JSONDecodeError, TypeError):
                    module = None
                if module in entry_modules:
                    await self.execution.cancel(order.client_order_id)
                    cancelled += 1
            self.log.info("trading paused (rest)", rest_reason=rest_reason,
                          cancelled_entry_orders=cancelled)

    # ── 决策 → 订单意图映射 ───────────────────────────────────
    async def _dispatch_decision(self, rec, d) -> None:
        if d.action == "CANCEL":
            n = await self.execution.cancel_all_market(rec.market_id)
            if n:
                self.log.info("cancelled orders", market=rec.market_id, n=n)
            return
        token_map = {
            "UP": rec.token_up,
            "DOWN": rec.token_down,
        }
        token_side = d.token_side
        if token_side not in token_map:
            if d.action == "ARB" and d.token_side == "BOTH":
                await self._dispatch_arb(rec, d)
            return
        ttl_s = float(self.config.s("entry.entry_order_ttl_s", 20))
        t_end_ms = rec.t_end * 1000
        if d.action.startswith("ENTER"):
            expires = min(t_end_ms - 15_000, now_ms() + int(ttl_s * 1000))
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="BUY",
                price=Decimal(d.price), qty=Decimal(d.qty), tif="GTD", post_only=True,
                expires_at_ms=expires, meta={"module": "entry", "token_side": token_side},
            )
        elif d.action.startswith("HEDGE"):
            expires = t_end_ms - 5_000
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="BUY",
                price=Decimal(d.price), qty=Decimal(d.qty), tif="GTD", post_only=True,
                expires_at_ms=expires, meta={"module": "hedge", "token_side": token_side},
            )
        elif d.action.startswith("EXIT"):
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="SELL",
                price=Decimal(d.price), qty=Decimal(d.qty), tif=d.tif or "FAK",
                post_only=d.post_only,
                expires_at_ms=t_end_ms - 5_000 if d.tif == "GTD" else None,
                meta={"module": "exit", "token_side": token_side},
            )
        elif d.action.startswith("TAIL_CAPTURE"):
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="BUY",
                price=Decimal(d.price), qty=Decimal(d.qty), tif="GTD", post_only=True,
                expires_at_ms=t_end_ms - 5_000,
                meta={"module": "tail_capture", "token_side": token_side},
            )
        elif d.action.startswith("XRP_FADE"):
            tif = d.tif or "GTD"
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="BUY",
                price=Decimal(d.price), qty=Decimal(d.qty), tif=tif, post_only=d.post_only,
                expires_at_ms=t_end_ms - 5_000 if tif == "GTD" else None,
                meta={"module": "xrp_fade", "token_side": token_side},
            )
        elif d.action.startswith("MID_CAPTURE"):
            # 中段实验：挂单寿命受 order_ttl_s 限制（防长期阻塞主 tail），上限窗口结束前 5s
            ttl_ms = int(float(self.config.s("mid_capture.order_ttl_s", 30)) * 1000)
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="BUY",
                price=Decimal(d.price), qty=Decimal(d.qty), tif="GTD", post_only=True,
                expires_at_ms=min(t_end_ms - 5_000, now_ms() + ttl_ms),
                meta={"module": "mid_capture", "token_side": token_side,
                      "exit_mode": d.exit_mode or "hold"},
            )
        else:
            self.log.warning("unmapped decision", action=d.action)
            return
        ok, reason = await self.execution.submit(intent)
        if not ok:
            self.log.warning("order rejected", action=d.action, reason=reason)
            if d.action.startswith("ENTER"):
                self.strategy.on_order_expired(rec.market_id)

    async def _dispatch_arb(self, rec, d) -> None:
        up_price, down_price = d.price.split("/")
        qty = Decimal(d.qty)
        intents = [
            OrderIntent(market_id=rec.market_id, token_id=rec.token_up, side="BUY",
                        price=Decimal(up_price), qty=qty, tif="FOK", post_only=False,
                        meta={"module": "arb", "leg": "up"}),
            OrderIntent(market_id=rec.market_id, token_id=rec.token_down, side="BUY",
                        price=Decimal(down_price), qty=qty, tif="FOK", post_only=False,
                        meta={"module": "arb", "leg": "down"}),
        ]
        results = []
        for intent in intents:
            ok, reason = await self.execution.submit(intent)
            results.append((intent, ok, reason))
        if any(not ok for _, ok, _ in results):
            self.log.warning("arb partial submit", results=[(i.client_order_id, ok) for i, ok, _ in results])
            for intent, ok, _ in results:
                if ok:
                    await self.execution.cancel(intent.client_order_id)

    async def _watchdog(self) -> None:
        max_gap = self.config.e("watchdog_max_gap_s", 3.0)
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            gap = time.monotonic() - self._last_heartbeat
            if gap > max_gap:
                self.log.error("WATCHDOG: main loop stalled", gap_s=round(gap, 2))

    # ── 启动/停止 ────────────────────────────────────────────
    def _rebuild_ptb(self, asset: str, t_start_s: int, lookback_s: int) -> Decimal | None:
        """RTDS 断流/重启兜底：用 1s spot 缓冲重建窗口起点 TWAP。"""
        buf = self.features.tick_buffers.get(asset)
        if buf is None:
            return None
        return rebuild_ptb_from_buffer(buf, t_start_s, lookback_s)

    async def _settle_market_by_gamma(self, market_id: int, result: str) -> None:
        """自结算被搁置/缺失时按 gamma 官方结果结账（结算权威=官方）。"""
        try:
            await self.execution.cancel_all_market(market_id)
        except Exception:
            pass
        pnl = Decimal(await self.repo.settle_positions(market_id, result))
        self._bt_session_pnl += pnl  # 止盈目标计数（北京日累计）
        self.strategy.on_settlement(market_id, result, pnl)
        self._settled_accounted.add(market_id)
        self.log.info("market settled by gamma", market=market_id, result=result, pnl=str(pnl))

    async def start(self) -> None:
        init_db(self.config.settings.db_url)
        await create_schema()
        self.log.info("supervisor starting", **self.config.sanitized_summary())

        # 实盘启动安全：撤掉上次崩溃可能残留的全部挂单 + 权益从链上余额读取。
        # 铁律：权益读取失败 = 拒绝启动（绝不带着占位权益 1000 去交易）。
        if isinstance(self.gateway, LiveGateway):
            await self.gateway.startup_safety()
            equity = await self.gateway.get_equity()
            if equity <= 0:
                raise RuntimeError(f"live equity invalid ({equity}); refusing to start")
            self.risk.set_equity(equity)  # 覆盖起始资金并重算亏损限额
            self.log.info("live equity loaded from collateral balance", equity=str(equity))

        # 启动即注册当前窗口（避免等下一个边界）
        now = now_s()
        # 重启回填：DB 恢复最近 6 分钟 spot tick 到缓冲 → PTB 重建兜底可用
        since_ms = now_ms() - 360_000
        for name in self.config.enabled_assets():
            try:
                rows = await self.repo.get_recent_ticks(name, since_ms)
                buf = self.features.tick_buffers[name]
                for ts_ms, price in rows:
                    buf.on_price(ts_ms, price)
                if rows:
                    self.log.info("tick buffer prefilled from db", asset=name, n=len(rows))
            except Exception:
                self.log.exception("tick prefill failed", asset=name)
        for name, acfg in self.config.enabled_assets().items():
            t_start = (now // acfg.duration_s) * acfg.duration_s
            win = AssetWindow(asset=name, tf_label=acfg.tf_label, t_start=t_start, duration_s=acfg.duration_s)
            rec = self.registry.get(name, t_start) or await self.registry.register_window(name, acfg, win)
            if rec is not None:
                self._last_window[name] = t_start
                lookback = rec.twap_lookback_s or acfg.twap_lookback_default
                self.twap.on_window_start(acfg, win, lookback)
                self.features.tick_buffers[name].reset_window(t_start)
                self.features.set_active_tokens(rec.token_up, rec.token_down)
                # 窗口已进行中：RTDS 无回放 → 先尝试 spot 重建兜底，失败才标记缺失
                if now - t_start > 10:
                    wt = self.twap.current(name)
                    if wt is not None and wt.ptb is None and wt.self_result is None:
                        rebuilt = self._rebuild_ptb(name, t_start, lookback)
                        if rebuilt is not None:
                            wt.ptb = rebuilt
                            wt.ptb_obs_ms = t_start * 1000
                            wt.ptb_src = "spot_rebuilt"
                            self.log.info("PTB captured (spot rebuilt at startup)", asset=name,
                                          t_start=t_start, ptb=str(rebuilt))
                        else:
                            wt.ptb_src = "missing"
                            self.log.error("PTB missing at startup (no RTDS replay)", asset=name,
                                           t_start=t_start)
            # 回补上一窗口：重启后仍能对其做 gamma 对账（结算记录在 DB，跨进程持久）
            prev_start = t_start - acfg.duration_s
            if self.registry.get(name, prev_start) is None:
                prev_win = AssetWindow(asset=name, tf_label=acfg.tf_label,
                                       t_start=prev_start, duration_s=acfg.duration_s)
                await self.registry.register_window(name, acfg, prev_win)

        self.market_ws.set_asset_ids(self.registry.subscription_tokens())

        self._tasks = [
            asyncio.create_task(self.rtds.run()),
            asyncio.create_task(self.market_ws.run()),
            asyncio.create_task(self.binance.run()),
            asyncio.create_task(self.repo.flush_loop()),
            asyncio.create_task(self._window_loop()),
            asyncio.create_task(self._housekeeping_loop()),
            asyncio.create_task(self._twap_timeout_loop()),
            asyncio.create_task(self._tick_persist_loop()),
            asyncio.create_task(self._status_loop()),
            asyncio.create_task(self._strategy_loop()),
            asyncio.create_task(self._execution_maintain_loop()),
            asyncio.create_task(self._rest_book_loop()),
            asyncio.create_task(self._watchdog()),
        ]

    async def _execution_maintain_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            try:
                await self.execution.maintain()
            except Exception:
                self.log.exception("execution maintain failed")

    # ── REST 簿口轮询（决策数据源，优于 WS 流的投递延迟） ──────
    async def _rest_book_loop(self) -> None:
        if not bool(self.config.s("books.rest_poll_enabled", True)):
            return
        interval = float(self.config.s("books.rest_poll_interval_s", 1.0))
        timeout = float(self.config.s("books.rest_poll_timeout_s", 5.0))
        self.log.info("REST book polling enabled", interval_s=interval)
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (compatible; pm5hft/0.1)"},
        )
        fails = 0
        try:
            while not self._stop.is_set():
                try:
                    await self._poll_rest_books(session)
                    fails = 0
                except Exception:
                    fails += 1
                    self.log.exception("rest book poll failed")
                    if fails >= 3:
                        # 连续失败 → 回退 WS 流簿口（数据虽旧但不断流）
                        self.features.clear_rest_tokens()
                        fails = 0
                        self.log.warning("REST book polling degraded, falling back to WS stream")
                await asyncio.sleep(interval)
        finally:
            await session.close()

    async def _poll_rest_books(self, session: aiohttp.ClientSession) -> None:
        tokens = self.registry.subscription_tokens()
        if not tokens:
            return
        async with session.post(
            "https://clob.polymarket.com/books",
            data=json.dumps([{"token_id": t} for t in tokens]),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"rest books status {resp.status}")
            data = await resp.json()
        if not isinstance(data, list):
            return
        self.features.set_rest_tokens(tokens)  # REST 为主数据源：流簿口更新对其跳过
        for b in data:
            if not isinstance(b, dict):
                continue
            token_id = b.get("asset_id") or b.get("token_id") or ""
            if not token_id:
                continue
            bids = [(x.get("price"), x.get("size")) for x in (b.get("bids") or [])]
            asks = [(x.get("price"), x.get("size")) for x in (b.get("asks") or [])]
            self.features.apply_rest_book(token_id, bids, asks)

    async def run(self) -> None:
        await self.start()
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        self.log.info("shutting down")
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.rtds.stop()
        await self.market_ws.stop()
        await self.binance.stop()
        await self.repo._flush()
        await close_db()
        self.log.info("shutdown complete")
