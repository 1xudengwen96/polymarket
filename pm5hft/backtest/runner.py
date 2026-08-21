"""回测回放器（docs/08 §2）：与实盘共用特征/概率/风控/策略/执行内核。

两种模式：
- collect：BaselineModel 驱动，按采样偏移记录特征数据集（模型训练用）；
- backtest：加载训练产物（逻辑回归 + 校准桶）做正式回放，产出报告。

严格防 lookahead：回放指针逐秒推进，只允许读取 <= now 的数据；标签仅用于结算。
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from ..config import Config
from ..db import close_db, create_schema, init_db, session_factory
from ..execution import ExecutionEngine, OrderIntent
from ..features import FeatureStore
from ..logging_setup import get_logger
from ..market_registry import MarketRecord
from ..models import FeatureRow, Market, MarketLabel, PriceHistory
from ..persistence import Repo
from ..probability.calibration import Calibrator
from ..probability.edge import compute_edge
from ..probability.model import BaselineModel
from ..risk import RiskEngine
from ..strategy import StrategyEngine
from .data import load_bars_conn, twap_from_bars
from .fillsim import BacktestGateway

BOOK_STALE_S = 30.0  # 回测簿口停滞守卫（镜像实盘 book_stale 熔断）

FEATURE_COLS = [
    "dist_bps", "remaining_s", "into_window_s",
    "ret_1s", "ret_3s", "ret_5s", "ret_10s", "ret_30s", "ret_60s",
    "rv_5s", "rv_30s", "rv_60s",
    "vol_1s", "vol_10s", "vol_60s",
    "agg_buy_5s", "agg_sell_5s", "agg_buy_30s", "agg_sell_30s",
    "tfi_5s", "tfi_30s", "cvd",
    "accel_5s", "reversal_score",
    "up_bid", "up_ask", "down_bid", "down_ask", "up_spread", "down_spread",
    "obi3", "obi10", "up_microprice",
    "pm_last_up", "pm_chg_60s",
]


@dataclass
class BacktestTwap:
    """模拟 WindowTwap 的最小接口（策略引擎依赖）。"""
    ptb: Decimal | None = None
    self_result: str | None = None
    samples: deque = field(default_factory=lambda: deque(maxlen=600))


class TrainedLogisticModel:  # noqa: D101 — 历史导入兼容，定义已移至 probability/model.py
    from ..probability.model import TrainedLogisticModel as _Base

    def __init__(self, features: list[str], coef: list[float], intercept: float) -> None:
        self._impl = self._Base(features, coef, intercept)

    def predict(self, f: dict) -> float:
        return self._impl.predict(f)


class BacktestRunner:
    def __init__(self, config: Config, mode: str = "backtest") -> None:
        self.config = config
        self.mode = mode
        self.log = get_logger("backtest.runner")

    # ── 主流程 ───────────────────────────────────────────────
    async def run(
        self,
        db_path: str,
        max_windows: int = 0,
        sample_offsets: tuple[int, ...] = (60, 120, 180, 240),
        model=None,
        calibrator: Calibrator | None = None,
        collect_split: str = "train",
        report_path: str | None = None,
        label_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        init_db(f"sqlite+aiosqlite:///{db_path}")
        await create_schema()
        async with session_factory()() as sess:
            await sess.execute(text("PRAGMA journal_mode=WAL"))
        if self.mode == "backtest":
            # 每次回放前清空交易痕迹（决策/订单/成交/持仓），保证指标干净
            from sqlalchemy import delete

            from ..models import Fill, Order, Position

            async with session_factory()() as sess:
                for tbl in (Fill, Order, Position):
                    await sess.execute(delete(tbl))
                await sess.commit()
        self.repo = Repo()
        self.risk = RiskEngine(self.config, mode="paper")
        self.calibrator = calibrator or Calibrator(
            min_n=int(self.config.p("min_bucket_n", 200)),
            cold_start_shrink=float(self.config.p("cold_start_shrink", 0.85)),
        )
        self.model = model or BaselineModel()
        self.strategy = StrategyEngine(self.config, self.risk, self.model, self.calibrator)
        self.features = FeatureStore(self.config)
        self.gateway = BacktestGateway(self.config, self.repo, self.features)
        # fill_handler 在 execution 创建后指向 engine.handle_fill（见 exec_maintain）

        markets, labels, price_hist = await self._load(db_path, max_windows)
        self.log.info("backtest replay", mode=self.mode, windows=len(markets))

        equity_curve: list[tuple[int, float]] = []
        per_window_pnl: list[float] = []
        self._reason_counts: dict[str, int] = {}
        self._decision_counts: dict[str, int] = {}
        self._max_gross = -1.0
        self._max_net = -1.0
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=10000")

        for i, m in enumerate(markets):
            result = await self._replay_window(conn, m, labels, price_hist, sample_offsets, collect_split)
            if result is None:
                continue
            equity_curve.append((m.t_end * 1000, float(self.risk.equity)))
            if result["pnl"] is not None:
                per_window_pnl.append(float(result["pnl"]))
            if (i + 1) % 100 == 0:
                self.log.info("replay progress", done=i + 1, total=len(markets),
                              equity=str(self.risk.equity))
        conn.close()
        await self.repo._flush()

        top_reasons = sorted(self._reason_counts.items(), key=lambda kv: -kv[1])[:10]
        self.log.info("decision reasons", reasons=top_reasons,
                      max_gross=f"{self._max_gross:.4f}", max_net=f"{self._max_net:.4f}",
                      max_ctx=getattr(self, "_max_ctx", None))
        stats = await self._collect_stats()
        metrics = self._metrics(equity_curve, per_window_pnl, stats)
        metrics["label_stats"] = label_stats or {}
        await close_db()
        if report_path:
            from .report import write_markdown_report

            write_markdown_report(report_path, metrics, self.calibrator)
        return metrics

    # ── 单窗口回放 ───────────────────────────────────────────
    async def _replay_window(self, conn, m: Market, labels: dict[int, dict],
                             price_hist: dict[int, list], sample_offsets: tuple[int, ...],
                             collect_split: str) -> dict[str, Any] | None:
        lbl = labels.get(m.id)
        # 标签不确定性过滤：|margin| < 阈值 的窗口（平局附近）不参与交易/训练。
        # 阈值可经 --so backtest.margin_filter_bps=0 关闭（量化 lookahead 过滤的贡献）
        margin_limit = float(self.config.s("backtest.margin_filter_bps", 1.5))
        if lbl and lbl.get("margin_bps") is not None and abs(lbl["margin_bps"]) < margin_limit:
            return None
        if self.mode == "backtest":
            result_src = None
            if lbl and lbl.get("gamma_result"):
                result_src = lbl["gamma_result"]
            elif lbl and lbl.get("twap_result"):
                result_src = lbl["twap_result"]
            if result_src is None:
                return None
        else:
            # collect 模式只需要标签（训练目标）
            if not (lbl and lbl.get("gamma_result")):
                return None

        rec = MarketRecord(
            market_id=m.id, event_id=m.event_id, slug=m.slug, asset=m.asset,
            t_start=m.t_start, t_end=m.t_end, condition_id=m.condition_id,
            token_up=m.token_up, token_down=m.token_down,
            twap_lookback_s=m.twap_lookback_s or 60,
            tick_size=m.tick_size or "0.01", min_order_size=m.min_order_size or "5",
            neg_risk=m.neg_risk,
        )
        wt = BacktestTwap()
        if lbl and lbl.get("twap_ptb"):
            wt.ptb = Decimal(lbl["twap_ptb"])
        else:
            bars_all = load_bars_conn(conn, (m.t_start - 90) * 1000, m.t_start * 1000 + 4000)
            v = twap_from_bars(bars_all, m.t_start * 1000, 60)
            wt.ptb = v
        if wt.ptb is None:
            return None

        # 数据切片（ticks：窗口前 60s 到窗口结束；pm 序列：窗口内）
        bars = load_bars_conn(conn, (m.t_start - 60) * 1000, m.t_end * 1000)
        series = [tuple(x) for x in price_hist.get(m.id, [])]

        buf = self.features.tick_buffers["btc"]
        buf.bars.clear()
        buf.cur = None
        buf.reset_window(m.t_start)
        self.features.books.pop(rec.token_up, None)
        self.features.books.pop(rec.token_down, None)
        self.features.pm_trades.pop(rec.token_up, None)
        self.features.pm_trades.pop(rec.token_down, None)
        self.features.set_active_tokens(rec.token_up, rec.token_down)

        twap_win: deque[float] = deque(maxlen=60)
        bar_idx = 0
        series_idx = 0
        decisions: list[dict] = []
        last_decision_ms = 0

        step = 1000
        for now in range((m.t_start - 60) * 1000, m.t_end * 1000, step):
            # 喂 ticks（防 lookahead：只喂 <= now 的 bar）
            while bar_idx < len(bars) and bars[bar_idx][0] <= now:
                ts, price = bars[bar_idx]
                bar_idx += 1
                # 用前后 bar 近似 o/h/l
                prev_p = bars[bar_idx - 2][1] if bar_idx >= 2 else price
                next_p = bars[bar_idx][1] if bar_idx < len(bars) else price
                buf.feed_bar(ts, prev_p, max(prev_p, price, next_p), min(prev_p, price, next_p),
                             price, 0.0, 0.0, 0.0, 1)
            # 喂 pm 概率序列（合成簿 + 成交流 + 市场动量）
            while series_idx < len(series) and series[series_idx][0] <= now:
                ts, p_up = series[series_idx]
                series_idx += 1
                p_up_f = float(p_up)
                await self.gateway.feed_series_point(rec.token_up, p_up_f, ts)
                await self.gateway.feed_series_point(rec.token_down, 1.0 - p_up_f, ts)
                await self.features.on_last_trade({"asset_id": rec.token_up, "price": str(p_up),
                                                   "size": "1", "timestamp": str(ts)})
            # 运行 TWAP60（1s bar 收盘均值，近似）
            if bar_idx > 0:
                twap_win.append(bars[bar_idx - 1][1])
                if len(twap_win) == 60:
                    wt.samples.append((now, Decimal(str(sum(twap_win) / 60))))

            self._replay_now = now  # 回测时钟（执行引擎/撮合器共用）
            await self.exec_maintain(rec)

            # 策略 tick：窗口开始后每秒（决策去抖：非 NOOP 至少间隔 1s）
            if now >= m.t_start * 1000 and now - last_decision_ms >= 1000:
                # 簿口停滞守卫：序列超过 BOOK_STALE_S 无更新 → 禁交易（镜像实盘熔断）
                last_series = self.gateway.last_series_ts.get(rec.token_up, 0)
                if now - last_series > BOOK_STALE_S * 1000:
                    self._reason_counts["book stale"] = self._reason_counts.get("book stale", 0) + 1
                    continue
                d, f, cal_p, market_p, edge = self._strategy_tick(rec, wt, now)
                last_decision_ms = now
                if d.action != "NOOP":
                    decisions.append({
                        "ts_ms": now, "action": d.action, "price": d.price, "qty": d.qty,
                        "cal_p": f"{cal_p:.4f}", "net_edge": f"{edge.net_edge:.4f}" if edge else None,
                    })
                    self._decision_counts[d.action] = self._decision_counts.get(d.action, 0) + 1
                    await self._dispatch(rec, d, now)
                else:
                    self._reason_counts[d.reason or "?"] = self._reason_counts.get(d.reason or "?", 0) + 1
                if self.mode == "collect" and now - m.t_start * 1000 in [o * 1000 for o in sample_offsets]:
                    await self._save_feature_row(m, now, f, lbl["gamma_result"], collect_split)

        # 结算
        result = lbl["gamma_result"] if self.mode == "collect" else (
            lbl.get("gamma_result") or lbl.get("twap_result")
        )
        pnl = None
        if result in ("UP", "DOWN"):
            pnl = Decimal(await self.repo.settle_positions(m.id, result))
            self.strategy.on_settlement(m.id, result, pnl)
        wt.self_result = result
        return {"pnl": str(pnl) if pnl is not None else None,
                "decisions": decisions}

    async def exec_maintain(self, rec: MarketRecord) -> None:
        if not hasattr(self, "execution"):
            self._t_end_map: dict[int, int] = {}
            self._replay_now = 0

            def _closed(_oid: str, market_id: int, _state: str) -> None:
                # 挂单过期/取消：策略回到可重新评估状态
                self.strategy.on_order_expired(market_id)

            self.execution = ExecutionEngine(
                self.config, self.repo, self.gateway,
                on_fill=self._handle_fill,
                deadline_for=lambda mid: self._t_end_map.get(mid),
                on_order_closed=_closed,
                now_ms_fn=lambda: self._replay_now,  # 回测时钟注入
            )
            self.gateway.fill_handler = self.execution.handle_fill
        self._t_end_map[rec.market_id] = rec.t_end * 1000
        await self.execution.maintain()

    def _strategy_tick(self, rec: MarketRecord, wt: BacktestTwap, now_ms_val: int):
        f = self.features.features(rec.asset, rec, wt, now_ms_val=now_ms_val)
        raw_p = self.model.predict(f)
        cal = self.calibrator.calibrate(raw_p)
        up_bid, up_ask = f.get("up_bid"), f.get("up_ask")
        market_p = (float(up_bid) + float(up_ask)) / 2.0 if up_bid is not None and up_ask is not None else None
        edge = None
        if market_p is not None:
            edge = compute_edge(
                cal.cal_prob, market_p, float(self.config.s("fees.taker_fee_bps", 1.0)) / 1e4,
                float(f.get("remaining_s") or 60.0),
                float(self.config.p("model_err_cold_start", 0.03)),
                float(self.config.p("risk_buffer_global", 0.005)),
            )
            self._max_gross = max(self._max_gross, edge.gross_edge)
            self._max_net = max(self._max_net, edge.net_edge)
            if edge.gross_edge == self._max_gross:
                self._max_ctx = {"remaining": float(f.get("remaining_s") or 0),
                                 "into": float(f.get("into_window_s") or 0),
                                 "cal": cal.cal_prob, "market": market_p}
        d = self.strategy.decide(f, rec, wt)
        return d, f, cal.cal_prob, market_p, edge

    async def _dispatch(self, rec: MarketRecord, d, now: int) -> None:
        if d.action == "CANCEL":
            await self.execution.cancel_all_market(rec.market_id)
            return
        token_map = {"UP": rec.token_up, "DOWN": rec.token_down}
        token_side = d.token_side
        if token_side == "BOTH":
            await self._dispatch_arb(rec, d)
            return
        if token_side not in token_map:
            return
        ttl_s = float(self.config.s("entry.entry_order_ttl_s", 20))
        t_end_ms = rec.t_end * 1000
        if d.action.startswith("ENTER"):
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="BUY",
                price=Decimal(d.price), qty=Decimal(d.qty), tif="GTD", post_only=True,
                expires_at_ms=min(t_end_ms - 15_000, now + int(ttl_s * 1000)),
                meta={"module": "entry", "token_side": token_side},
            )
        elif d.action.startswith("HEDGE"):
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="BUY",
                price=Decimal(d.price), qty=Decimal(d.qty), tif="GTD", post_only=True,
                expires_at_ms=t_end_ms - 5_000, meta={"module": "hedge", "token_side": token_side},
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
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token_map[token_side], side="BUY",
                price=Decimal(d.price), qty=Decimal(d.qty), tif="GTD", post_only=True,
                expires_at_ms=t_end_ms - 5_000,
                meta={"module": "mid_capture", "token_side": token_side,
                      "exit_mode": d.exit_mode or "hold"},
            )
        else:
            return
        ok, reason = await self.execution.submit(intent)
        if not ok and d.action.startswith("ENTER"):
            self.strategy.on_order_expired(rec.market_id)

    async def _dispatch_arb(self, rec: MarketRecord, d) -> None:
        up_price, down_price = d.price.split("/")
        qty = Decimal(d.qty)
        for token, price, leg in ((rec.token_up, up_price, "up"), (rec.token_down, down_price, "down")):
            intent = OrderIntent(
                market_id=rec.market_id, token_id=token, side="BUY",
                price=Decimal(price), qty=qty, tif="FOK", post_only=False,
                meta={"module": "arb", "leg": leg},
            )
            await self.execution.submit(intent)

    # ── 成交回写 ─────────────────────────────────────────────
    async def _handle_fill(self, fill) -> None:
        rec = self._rec_for_token.get(fill.token_id)
        if rec is None:
            return
        token_side = "UP" if fill.token_id == rec.token_up else "DOWN"
        order = await self.repo.get_order(fill.order_id)
        meta: dict[str, Any] = {}
        if order is not None and order.meta:
            try:
                meta = json.loads(order.meta)
            except json.JSONDecodeError:
                meta = {}
        if meta.get("module") == "entry":
            pos = self.strategy.positions.get(rec.id)
            if pos is not None:
                held = pos.up_qty if token_side == "UP" else pos.down_qty
                meta["first_fill"] = held <= 0
        self.strategy.on_fill(rec.id, token_side, fill.qty, fill.price, fill.side, meta)

    # ── 数据加载 ─────────────────────────────────────────────
    async def _load(self, db_path: str, max_windows: int):

        from ..db import session_factory

        async with session_factory()() as sess:
            markets = list((await sess.execute(
                select(Market).where(Market.asset == "btc").order_by(Market.t_start)
            )).scalars().all())
            lbl_rows = list((await sess.execute(select(MarketLabel))).scalars().all())
            ph_rows = list((await sess.execute(
                select(PriceHistory).where(PriceHistory.side.in_(("UP", None)))
                .order_by(PriceHistory.ts_ms)
            )).scalars().all())
        labels = {}
        for r in lbl_rows:
            margin = None
            if r.twap_margin_bps:
                try:
                    margin = float(r.twap_margin_bps)
                except ValueError:
                    margin = None
            labels[r.market_id] = {
                "gamma_result": r.gamma_result, "twap_ptb": r.twap_ptb,
                "twap_final": r.twap_final, "twap_result": r.twap_result,
                "margin_bps": margin,
            }
        price_hist: dict[int, list] = {}
        for r in ph_rows:
            price_hist.setdefault(r.market_id, []).append((r.ts_ms, float(r.price)))
        if max_windows:
            markets = markets[:max_windows]
        self._rec_for_token = {}
        for m in markets:
            self._rec_for_token[m.token_up] = m
            self._rec_for_token[m.token_down] = m
        return markets, labels, price_hist

    # ── 统计与指标 ───────────────────────────────────────────
    async def _collect_stats(self) -> dict[str, Any]:
        from sqlalchemy import func, select

        from ..models import Fill, Order

        async with session_factory()() as sess:
            n_orders = (await sess.execute(select(func.count(Order.client_order_id)))).scalar_one()
            n_fills = (await sess.execute(select(func.count(Fill.id)))).scalar_one()
        return {"n_orders": n_orders, "n_fills": n_fills,
                "decisions": dict(self._decision_counts)}

    def _metrics(self, equity_curve, per_window_pnl, stats) -> dict[str, Any]:
        import math

        m: dict[str, Any] = {
            "n_windows_traded": len([p for p in per_window_pnl if p != 0]),
            "n_orders": stats.get("n_orders", 0),
            "n_fills": stats.get("n_fills", 0),
            "decisions": stats.get("decisions", {}),
            "total_pnl": sum(per_window_pnl),
            "equity_final": equity_curve[-1][1] if equity_curve else 10000.0,
        }
        wins = [p for p in per_window_pnl if p > 0]
        losses = [p for p in per_window_pnl if p < 0]
        m["wins"] = len(wins)
        m["losses"] = len(losses)
        m["win_rate"] = len(wins) / max(1, len(wins) + len(losses))
        m["avg_win"] = sum(wins) / len(wins) if wins else 0.0
        m["avg_loss"] = sum(losses) / len(losses) if losses else 0.0
        m["expectancy"] = sum(per_window_pnl) / max(1, len(per_window_pnl))
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        m["profit_factor"] = gross_win / gross_loss if gross_loss > 0 else float("inf")
        # 回撤
        peak = -1e18
        mdd = 0.0
        for _, eq in equity_curve:
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
        m["max_drawdown"] = mdd
        # sharpe/sortino（每窗口收益，年化因子 sqrt(288 窗口/日) 不适用 → 用每窗口 std）
        mean_p = sum(per_window_pnl) / max(1, len(per_window_pnl))
        var = sum((p - mean_p) ** 2 for p in per_window_pnl) / max(1, len(per_window_pnl) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        m["sharpe"] = (mean_p / std * math.sqrt(288)) if std > 0 else 0.0
        downs = [p - mean_p for p in per_window_pnl if p < mean_p]
        dvar = sum(x * x for x in downs) / max(1, len(downs))
        dstd = math.sqrt(dvar)
        m["sortino"] = (mean_p / dstd * math.sqrt(288)) if dstd > 0 else 0.0
        return m

    # ── 特征集 ───────────────────────────────────────────────
    async def _save_feature_row(self, m: Market, ts_ms: int, f: dict, label: str, split: str) -> None:
        from sqlalchemy import delete

        from ..db import session_factory

        offset_s = (ts_ms // 1000) - m.t_start
        async with session_factory()() as sess:
            # 幂等：同一 (market, offset) 先删后插
            await sess.execute(delete(FeatureRow).where(
                FeatureRow.market_id == m.id, FeatureRow.sample_offset_s == offset_s
            ))
            sess.add(FeatureRow(
                market_id=m.id, asset=m.asset, t_start=m.t_start,
                sample_offset_s=offset_s,
                label=1 if label == "UP" else 0,
                features=json.dumps({k: (f"{v:.6f}" if isinstance(v, float) else v)
                                     for k, v in f.items() if k in FEATURE_COLS}),
                split=split,
            ))
            await sess.commit()
