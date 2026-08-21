"""特征计算与环形缓存（回测/paper/live 三模式共用同一模块）。

特征定义见 docs/05-probability-engine.md §2。所有特征值 float 或 None（NaN 显式编码）。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..clock import now_ms
from ..config import Config
from ..logging_setup import get_logger


# ──────────────────────────────────────────────────────────────
# TickBuffer：逐笔 → 1s bars → 多尺度特征
# ──────────────────────────────────────────────────────────────
@dataclass
class Bar:
    ts_ms: int
    o: float
    h: float
    low: float
    c: float
    vol: float
    agg_buy: float
    agg_sell: float
    n: int


class TickBuffer:
    def __init__(self, max_bars: int = 360) -> None:
        self.bars: deque[Bar] = deque(maxlen=max_bars)
        self.cur: Bar | None = None
        self.cvd: float = 0.0
        self.window_start_ms: int | None = None
        self.last_tick_ms: int = 0

    def reset_window(self, t_start_s: int) -> None:
        self.cvd = 0.0
        self.window_start_ms = t_start_s * 1000

    def feed_bar(self, ts_ms: int, o: float, h: float, low: float, c: float,
                 vol: float, agg_buy: float, agg_sell: float, n: int) -> None:
        """回测用：直接喂入已聚合的 1s bar。"""
        self.bars.append(Bar(ts_ms=ts_ms, o=o, h=h, low=low, c=c, vol=vol,
                             agg_buy=agg_buy, agg_sell=agg_sell, n=n))
        self.cvd += agg_buy - agg_sell

    def on_trade(self, ts_ms: int, price: float, qty: float, is_buyer_maker: bool) -> None:
        agg_buy = 0.0 if is_buyer_maker else qty
        agg_sell = qty if is_buyer_maker else 0.0
        self._add(ts_ms, price, qty, agg_buy, agg_sell)
        self.last_tick_ms = ts_ms

    def on_price(self, ts_ms: int, price: float) -> None:
        """RTDS 后备参考价（无攻击方向信息）。"""
        self._add(ts_ms, price, 0.0, 0.0, 0.0)
        self.last_tick_ms = ts_ms

    def _add(self, ts_ms: int, price: float, vol: float, agg_buy: float, agg_sell: float) -> None:
        bucket = ts_ms - (ts_ms % 1000)
        if self.cur is None or bucket != self.cur.ts_ms:
            self._roll(bucket)
        assert self.cur is not None
        b = self.cur
        if b.n == 0:
            b.o = price
        b.h = max(b.h, price)
        b.low = min(b.low, price)
        b.c = price
        b.vol += vol
        b.agg_buy += agg_buy
        b.agg_sell += agg_sell
        b.n += 1
        self.cvd += agg_buy - agg_sell

    def _roll(self, new_bucket: int) -> None:
        if self.cur is not None:
            bar = self.cur
            if bar.n == 0:
                if self.bars:
                    # 空 bar 前向填充（价格未变，量=0）——禁止 0 价污染特征
                    prev_c = self.bars[-1].c
                    bar.o = bar.h = bar.low = bar.c = prev_c
                else:
                    bar = None  # 无历史可填：丢弃
            if bar is not None:
                self.bars.append(bar)
        self.cur = Bar(ts_ms=new_bucket, o=0.0, h=float("-inf"), low=float("inf"), c=0.0,
                       vol=0.0, agg_buy=0.0, agg_sell=0.0, n=0)

    def roll_to(self, now_ms_val: int) -> None:
        """把已完成的 1s bar 归档（周期调用）。"""
        bucket = now_ms_val - (now_ms_val % 1000)
        while self.cur is not None and self.cur.ts_ms < bucket:
            self._roll(self.cur.ts_ms + 1000)
        if self.cur is None:
            self._roll(bucket)

    def closes(self) -> list[float]:
        out = [b.c for b in self.bars]
        if self.cur is not None and self.cur.n > 0:
            out.append(self.cur.c)
        return out

    def _window(self, n: int, attr: str) -> list[float]:
        bars = list(self.bars)
        if self.cur is not None and self.cur.n > 0:
            bars.append(self.cur)
        return [getattr(b, attr) for b in bars[-n:] if getattr(b, attr) not in (float("-inf"), float("inf"))]

    def ret(self, n_s: int) -> float | None:
        """最近 n 秒收益率。"""
        closes = self.closes()
        if len(closes) < n_s + 1:
            return None
        base = closes[-n_s - 1]
        if base == 0:
            return None
        return closes[-1] / base - 1.0

    def rv(self, n_s: int) -> float | None:
        """已实现波动率（1s 收益标准差 × sqrt(窗口数)）。"""
        closes = self.closes()
        if len(closes) < n_s + 2:
            return None
        seg = closes[-(n_s + 1):]
        rets = [seg[i] / seg[i - 1] - 1.0 for i in range(1, len(seg)) if seg[i - 1] != 0]
        if len(rets) < 2:
            return None
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(len(rets))

    def vol(self, n_s: int) -> float:
        return sum(self._window(n_s, "vol"))

    def agg_flow(self, n_s: int) -> tuple[float, float]:
        buys = sum(self._window(n_s, "agg_buy"))
        sells = sum(self._window(n_s, "agg_sell"))
        return buys, sells

    def tfi(self, n_s: int) -> float | None:
        buys, sells = self.agg_flow(n_s)
        tot = buys + sells
        if tot == 0:
            return None
        return (buys - sells) / tot

    def accel(self, short_s: int = 5, long_s: int = 20) -> float | None:
        r_short = self.ret(short_s)
        r_long = self.ret(long_s)
        if r_short is None or r_long is None:
            return None
        return r_short - r_long

    def reversal_score(self, n_s: int = 60) -> float | None:
        """1 = 处于区间低位（潜在向上反转），0 = 处于区间高位。"""
        bars = self._window(n_s, "c")
        if len(bars) < 2:
            return None
        hi = max(bars)
        lo = min(bars)
        if hi == lo:
            return 0.5
        pos = (bars[-1] - lo) / (hi - lo)
        return 1.0 - pos

    def approx_twap(self, n_s: int) -> float | None:
        closes = self.closes()
        if len(closes) < max(2, n_s // 2):
            return None
        seg = closes[-n_s:]
        return sum(seg) / len(seg)


# ──────────────────────────────────────────────────────────────
# BookState：单 token 订单簿
# ──────────────────────────────────────────────────────────────
@dataclass
class BookState:
    token_id: str
    tick_size: str = "0.01"
    best_bid: float | None = None
    best_ask: float | None = None
    levels: dict[float, float] = field(default_factory=dict)  # price -> size
    hash: str | None = None
    updated_ms: int = 0

    def update_snapshot(self, bids: list, asks: list, hash_: str | None, ts_ms: int) -> None:
        # 防御性排序：bids 降序取头部，asks 升序取头部（REST 实测顺序与文档相反）
        self.levels = {}
        for p, s in bids:
            f = float(p)
            if f > 0:
                self.levels[f] = float(s)
        for p, s in asks:
            f = float(p)
            if f > 0:
                self.levels[f] = -float(s)  # ask 记为负 size 区分
        bid_prices = sorted((p for p, s in self.levels.items() if s > 0), reverse=True)
        ask_prices = sorted((p for p, s in self.levels.items() if s < 0))
        self.best_bid = bid_prices[0] if bid_prices else None
        self.best_ask = ask_prices[0] if ask_prices else None
        self.hash = hash_
        self.updated_ms = ts_ms

    def apply_price_change(self, price: str, size: str, side: str) -> None:
        try:
            p = float(price)
            s = float(size)
        except ValueError:
            return
        if side == "BUY":
            if s == 0:
                self.levels.pop(p, None)
            else:
                self.levels[p] = s
        elif side == "SELL":
            if s == 0:
                self.levels.pop(p, None)
            else:
                self.levels[p] = -s
        bid_prices = sorted((pp for pp, ss in self.levels.items() if ss > 0), reverse=True)
        ask_prices = sorted((pp for pp, ss in self.levels.items() if ss < 0))
        self.best_bid = bid_prices[0] if bid_prices else None
        self.best_ask = ask_prices[0] if ask_prices else None

    def top(self, n: int) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bids = sorted(((p, s) for p, s in self.levels.items() if s > 0), key=lambda x: -x[0])[:n]
        asks = sorted(((p, -s) for p, s in self.levels.items() if s < 0), key=lambda x: x[0])[:n]
        return bids, asks

    def obi(self, n: int) -> float | None:
        bids, asks = self.top(n)
        bv = sum(s for _, s in bids)
        av = sum(s for _, s in asks)
        tot = bv + av
        if tot == 0:
            return None
        return (bv - av) / tot

    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def microprice(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        b, a = self.best_bid, self.best_ask
        bs = self.levels.get(b, 0.0)
        as_ = -self.levels.get(a, 0.0)
        if bs + as_ == 0:
            return (b + a) / 2.0
        return (b * as_ + a * bs) / (bs + as_)


# ──────────────────────────────────────────────────────────────
# FeatureStore：聚合
# ──────────────────────────────────────────────────────────────
def _f(x: float | None) -> float | None:
    if x is None or math.isnan(x) or math.isinf(x):
        return None
    return float(x)


class FeatureStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.log = get_logger("features")
        self.tick_buffers: dict[str, TickBuffer] = {a: TickBuffer() for a in config.enabled_assets()}
        self.books: dict[str, BookState] = {}  # token_id -> BookState
        self.pm_trades: dict[str, deque] = {}  # token_id -> deque[(ts_ms, price, size)]
        self.last_book_ts: dict[str, int] = {}
        self.rest_tokens: set[str] = set()  # REST /books 掌管的 token（流簿口更新对其跳过）

    def set_rest_tokens(self, tokens) -> None:
        """REST 簿口轮询掌管的 token 集合（REST 为主数据源时设置）。"""
        self.rest_tokens = set(tokens)

    def clear_rest_tokens(self) -> None:
        """REST 轮询失败回退：清空后 WS 流簿口恢复更新。"""
        self.rest_tokens.clear()

    def book_age_ms(self, token_id: str, now: int | None = None) -> int | None:
        """最近一次簿口/price_change 距现在的毫秒数；从未收到过数据返回 None。"""
        ts = self.last_book_ts.get(token_id)
        if not ts:
            return None
        now = now if now is not None else now_ms()
        return max(0, now - ts)

    def apply_rest_book(self, token_id: str, bids: list, asks: list) -> None:
        """REST /books 快照（撮合引擎真实簿口，延迟 ~0.3-0.6s，远优于 WS 流）。

        与 on_book_event 同构：levels 全量替换 + 最优价重算；新鲜度同样用
        到达时刻（book_stale 熔断语义 = 我们停止收到更新）。REST 轮询失败时
        不调用本方法，WS 流簿口继续作为回退数据源。
        """
        book = self.books.setdefault(token_id, BookState(token_id=token_id))
        book.update_snapshot(bids, asks, None, now_ms())
        now = now_ms()
        book.updated_ms = now
        self.last_book_ts[token_id] = now

    # ── 数据入口 ─────────────────────────────────────────────
    async def on_trade(self, symbol: str, ts_ms: int, price: float, qty: float, is_buyer_maker: bool) -> None:
        for asset, buf in self.tick_buffers.items():
            if self.config.asset(asset).binance_symbol == symbol:
                buf.on_trade(ts_ms, price, qty, is_buyer_maker)
                return

    async def on_crypto_price(self, symbol: str, ts_ms: int, value) -> None:
        for asset, buf in self.tick_buffers.items():
            if self.config.asset(asset).rtds_binance_symbol == symbol:
                buf.on_price(ts_ms, float(value))
                return

    async def on_book_event(self, msg: dict[str, Any]) -> None:
        token_id = msg.get("asset_id") or ""
        if not token_id:
            return
        if token_id in self.rest_tokens:
            return  # REST 为主数据源：跳过流簿口（流数据过期 10-20s，会污染新鲜快照）
        ts = int(msg.get("timestamp") or 0) or now_ms()
        book = self.books.setdefault(token_id, BookState(token_id=token_id))
        bids = [(b.get("price"), b.get("size")) for b in (msg.get("bids") or [])]
        asks = [(a.get("price"), a.get("size")) for a in (msg.get("asks") or [])]
        book.update_snapshot(bids, asks, msg.get("hash"), ts)
        # 注意：不采纳 book 快照的 tick_size 字段——它是市场基础 tick，
        # 尾部市场（价格 ≥0.96）仍报 0.01，会持续覆盖 tick_size_change
        # 事件的 0.001，导致按 tick 计算的出场阈值放大 10 倍（曾致实盘
        # 尾单永不触发 +3tick 出场）。tick 一律走价格区域规则
        # （strategy.side_tick），tick_size_change 事件仅作日志参考。
        # 新鲜度用「到达时刻」而非消息生成时间戳：实测 CLOB 流传输延迟
        # 中位 2.9s / p95 16.7s，若用生成时间戳会把流延迟误判为簿口陈旧，
        # book_stale 熔断被常态误触发。熔断语义 = 我们的簿口停止收到更新
        # （订阅丢失/断流类故障），不是交易所下发延迟。
        now = now_ms()
        book.updated_ms = now
        self.last_book_ts[token_id] = now

    async def on_price_change(self, msg: dict[str, Any]) -> None:
        for pc in msg.get("price_changes") or []:
            token_id = pc.get("asset_id") or ""
            if not token_id or token_id not in self.books:
                continue
            if token_id in self.rest_tokens:
                continue  # REST 为主数据源：跳过流簿口（流数据过期 10-20s，会污染新鲜快照）
            book = self.books[token_id]
            book.apply_price_change(pc.get("price"), pc.get("size"), pc.get("side"))
            # 交易所随包携带的权威最优价：直接校准（档位簿漂移自愈）
            if pc.get("best_bid") is not None:
                book.best_bid = float(pc["best_bid"])
            if pc.get("best_ask") is not None:
                book.best_ask = float(pc["best_ask"])
            # 新鲜度用到达时刻（见 on_book_event 注释）
            now = now_ms()
            book.updated_ms = now
            self.last_book_ts[token_id] = now

    async def on_last_trade(self, msg: dict[str, Any]) -> None:
        token_id = msg.get("asset_id") or ""
        ts = int(msg.get("timestamp") or 0) or now_ms()
        dq = self.pm_trades.setdefault(token_id, deque(maxlen=500))
        dq.append((ts, float(msg.get("price") or 0), float(msg.get("size") or 0)))

    async def on_tick_size_change(self, msg: dict[str, Any]) -> None:
        asset_ids = msg.get("asset_ids") or [msg.get("asset_id")] or []
        new_tick = str(msg.get("new_tick_size") or "")
        for tid in asset_ids:
            if tid in self.books:
                self.books[tid].tick_size = new_tick
        self.log.warning("tick size change", tokens=[str(t)[:12] for t in asset_ids],
                         old=msg.get("old_tick_size"), new=new_tick)

    def set_active_tokens(self, token_up: str, token_down: str) -> None:
        """新窗口开始时注册簿口 key（旧簿口保留在内存，由窗口清理逻辑移除）。"""
        for t in (token_up, token_down):
            if t not in self.books:
                self.books[t] = BookState(token_id=t)

    # ── 特征快照 ─────────────────────────────────────────────
    def roll_bars(self, now_ms_val: int | None = None) -> None:
        now = now_ms_val if now_ms_val is not None else now_ms()
        for buf in self.tick_buffers.values():
            buf.roll_to(now)

    def features(self, asset: str, rec, wt, now_ms_val: int | None = None) -> dict[str, Any]:
        """rec: MarketRecord；wt: WindowTwap。输出 docs/05 §2 特征向量。

        now_ms_val: 回测用回放时间（live 用墙钟）。
        """
        buf = self.tick_buffers[asset]
        self.roll_bars(now_ms_val)
        now = now_ms_val if now_ms_val is not None else now_ms()
        self.config.asset(asset)

        up_book = self.books.get(rec.token_up)
        down_book = self.books.get(rec.token_down)

        # TWAP 距离
        twap_now = None
        if wt is not None and wt.samples:
            twap_now = float(wt.samples[-1][1])
        ptb = float(wt.ptb) if wt is not None and wt.ptb is not None else None
        dist_bps = None
        if twap_now is not None and ptb:
            dist_bps = (twap_now / ptb - 1.0) * 1e4

        pm_last_up = None
        pm_chg_60s = None
        dq = self.pm_trades.get(rec.token_up)
        if dq:
            pm_last_up = dq[-1][1]
            cutoff = now - 60_000
            past = [p for t, p, _ in dq if t >= cutoff]
            if past and past[0]:
                pm_chg_60s = pm_last_up / past[0] - 1.0

        f = {
            "ts_ms": now,
            "remaining_s": _f(max(0.0, rec.t_end - now / 1000)),
            "into_window_s": _f(max(0.0, now / 1000 - rec.t_start)),
            "ptb": ptb,
            "twap_now": twap_now,
            "dist_bps": _f(dist_bps),
            "approx_twap60": _f(buf.approx_twap(60)),
            "approx_twap30": _f(buf.approx_twap(30)),
            "btc_price": _f(buf.closes()[-1] if buf.closes() else None),
            "ret_1s": _f(buf.ret(1)), "ret_3s": _f(buf.ret(3)), "ret_5s": _f(buf.ret(5)),
            "ret_10s": _f(buf.ret(10)), "ret_30s": _f(buf.ret(30)), "ret_60s": _f(buf.ret(60)),
            "rv_5s": _f(buf.rv(5)), "rv_30s": _f(buf.rv(30)), "rv_60s": _f(buf.rv(60)),
            "vol_1s": _f(buf.vol(1)), "vol_10s": _f(buf.vol(10)), "vol_60s": _f(buf.vol(60)),
            "agg_buy_5s": _f(buf.agg_flow(5)[0]), "agg_sell_5s": _f(buf.agg_flow(5)[1]),
            "agg_buy_30s": _f(buf.agg_flow(30)[0]), "agg_sell_30s": _f(buf.agg_flow(30)[1]),
            "tfi_5s": _f(buf.tfi(5)), "tfi_30s": _f(buf.tfi(30)),
            "cvd": _f(buf.cvd),
            "accel_5s": _f(buf.accel(5, 20)),
            "reversal_score": _f(buf.reversal_score(60)),
            "tick_age_ms": now - buf.last_tick_ms if buf.last_tick_ms else None,
            "up_bid": up_book.best_bid if up_book else None,
            "up_ask": up_book.best_ask if up_book else None,
            "down_bid": down_book.best_bid if down_book else None,
            "down_ask": down_book.best_ask if down_book else None,
            "up_book_age_ms": (now - up_book.updated_ms) if up_book and up_book.updated_ms else None,
            "down_book_age_ms": (now - down_book.updated_ms) if down_book and down_book.updated_ms else None,
            "up_spread": _f(up_book.spread()) if up_book else None,
            "down_spread": _f(down_book.spread()) if down_book else None,
            "up_tick": up_book.tick_size if up_book and up_book.tick_size else None,
            "down_tick": down_book.tick_size if down_book and down_book.tick_size else None,
            "obi3": _f(up_book.obi(3)) if up_book else None,
            "obi10": _f(up_book.obi(10)) if up_book else None,
            "up_microprice": _f(up_book.microprice()) if up_book else None,
            "pm_last_up": pm_last_up,
            "pm_chg_60s": _f(pm_chg_60s),
        }
        # 清理空簿口 None（JSON 友好）
        return {k: v for k, v in f.items()}
