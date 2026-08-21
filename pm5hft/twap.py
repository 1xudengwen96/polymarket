"""TWAP 服务：PTB 捕获、结算变量追踪、自结算、gamma 对账。

结算定义（docs/00 §2，市场 description 原文）：
  Up ⇔ TWAP_L(t_end) >= TWAP_L(t_start)，L = twapLookbackSeconds（BTC 5m 当前 = 60）
PTB 无公开 API：只能从 RTDS TWAP 流在窗口边界采样（断线无重放 → 缺样即缺 PTB）。

竞态处理：窗口边界到达时旧窗口尚未结算 → 旧 WindowTwap 移入 pending 队列，
final 样本到达后仍在 pending 上完成结算。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .clock import AssetWindow, now_ms
from .config import AssetConfig, Config
from .logging_setup import get_logger

PTB_TIMEOUT_S = 15
PTB_FALLBACK_AT_S = 6  # RTDS 断流时，窗口开始 6 秒后尝试 spot 缓冲重建 PTB
FINAL_TIMEOUT_S = 20
FINAL_FALLBACK_AT_S = 8  # 窗口结束 8 秒后 final 仍缺失 → spot 缓冲重建
PENDING_MAX = 4


def rebuild_ptb_from_buffer(buf, t_start_s: int, lookback_s: int) -> Decimal | None:
    """从 1s spot 缓冲重建窗口起点 TWAP（RTDS 断流/重启兜底）。

    质检：序列首样本距窗口起点 ≤5s、末样本距窗口终点 ≤2s（首尾完整 → 截断样本拒绝）；
    内部缺秒按前值填充后在 1s 网格上取均值（现货 tick 偶发缺秒是常态）。
    """
    t0 = t_start_s * 1000 - lookback_s * 1000
    t1 = t_start_s * 1000
    seq = [(b.ts_ms, b.c) for b in buf.bars
           if t0 - 1000 <= b.ts_ms < t1 and b.c not in (0.0, float("-inf"), float("inf"))]
    if not seq:
        return None
    if seq[0][0] - t0 > 5000 or t1 - seq[-1][0] > 2000:
        return None
    grid: list[float] = []
    idx = 0
    last = seq[0][1]
    for ts in range(t0, t1, 1000):
        while idx < len(seq) and seq[idx][0] <= ts:
            last = seq[idx][1]
            idx += 1
        grid.append(last)
    return Decimal(str(sum(grid) / len(grid)))


@dataclass
class WindowTwap:
    asset: str
    t_start: int
    t_end: int
    lookback_s: int
    symbol: str
    ptb: Decimal | None = None
    ptb_obs_ms: int | None = None
    ptb_src: str | None = None
    final: Decimal | None = None
    final_obs_ms: int | None = None
    final_src: str | None = None
    self_result: str | None = None  # UP|DOWN|UNKNOWN
    settled_at_ms: int | None = None
    gamma_result: str | None = None
    reconciled: bool = False
    dispute: str | None = None
    samples: deque = field(default_factory=lambda: deque(maxlen=600))  # (obs_ms, Decimal)


class TwapService:
    def __init__(self, config: Config, repo, binance_twap=None, on_gamma_settle=None) -> None:
        self.config = config
        self.repo = repo
        self.log = get_logger("twap")
        # binance_twap(asset, t_start_s, lookback_s) -> Decimal | None（PTB 兜底重建器）
        self._binance_twap = binance_twap
        # on_gamma_settle(market_id, gamma_result)：自结算被搁置/缺失时按官方结果结账
        self._on_gamma_settle = on_gamma_settle
        # (symbol, window_s) -> 最近样本（诊断/跨窗口兜底）
        self._latest: dict[tuple[str, int], tuple[int, Decimal]] = {}
        # asset -> 当前窗口；asset -> 等待 final 的旧窗口队列
        self._windows: dict[str, WindowTwap] = {}
        self._pending: dict[str, deque[WindowTwap]] = {}

    # ── 流回调 ───────────────────────────────────────────────
    async def on_twap(self, symbol: str, window_s: int, value: Decimal, full_e18: str, obs_ts_ms: int) -> None:
        key = (symbol, window_s)
        self._latest[key] = (obs_ts_ms, value)
        await self.repo.insert_twap_sample(symbol, window_s, full_e18 or str(value), obs_ts_ms, src="rtds")

        targets: list[WindowTwap] = list(self._windows.values())
        for dq in self._pending.values():
            targets.extend(dq)
        for wt in targets:
            if wt.symbol != symbol or wt.lookback_s != window_s:
                continue
            wt.samples.append((obs_ts_ms, value))
            if wt.ptb is None and wt.self_result is None and obs_ts_ms >= wt.t_start * 1000:
                wt.ptb = value
                wt.ptb_obs_ms = obs_ts_ms
                wt.ptb_src = "rtds"
                self.log.info("PTB captured", asset=wt.asset, t_start=wt.t_start, ptb=str(value),
                              obs_ms=obs_ts_ms, lookback_s=window_s)
            if wt.ptb is not None and wt.final is None and wt.self_result is None and obs_ts_ms >= wt.t_end * 1000:
                wt.final = value
                wt.final_obs_ms = obs_ts_ms
                wt.final_src = "rtds"
                await self._settle(wt)

    # ── 窗口生命周期 ─────────────────────────────────────────
    def on_window_start(self, asset_cfg: AssetConfig, win: AssetWindow, lookback_s: int) -> None:
        asset = asset_cfg.asset
        old = self._windows.get(asset)
        if old is not None and old.t_start != win.t_start:
            # 旧窗口尚未结算 → 移入 pending 等待 final 样本
            dq = self._pending.setdefault(asset, deque(maxlen=PENDING_MAX))
            dq.append(old)
        self._windows[asset] = WindowTwap(
            asset=asset,
            t_start=win.t_start,
            t_end=win.t_end,
            lookback_s=lookback_s,
            symbol=asset_cfg.twap_symbol,
        )
        self.log.info("twap window opened", asset=asset, t_start=win.t_start,
                      lookback_s=lookback_s, symbol=asset_cfg.twap_symbol)

    def _find(self, asset: str, t_start: int) -> WindowTwap | None:
        cur = self._windows.get(asset)
        if cur is not None and cur.t_start == t_start:
            return cur
        for wt in self._pending.get(asset, []):
            if wt.t_start == t_start:
                return wt
        return None

    async def on_window_end(self, asset_cfg: AssetConfig, win: AssetWindow) -> WindowTwap | None:
        wt = self._find(asset_cfg.asset, win.t_start)
        if wt is None:
            self.log.warning("twap window end without tracking", asset=asset_cfg.asset, t_start=win.t_start)
            return None
        if wt.ptb is None:
            wt.ptb_src = "missing"
            wt.self_result = "UNKNOWN"
            wt.settled_at_ms = now_ms()
            await self._persist_settlement(wt)
            self.log.error("PTB MISSING: window untradable", asset=asset_cfg.asset, t_start=win.t_start)
            return wt
        if wt.final is None:
            wt.self_result = "UNKNOWN"
            wt.settled_at_ms = now_ms()
            await self._persist_settlement(wt)
            self.log.error("final TWAP missing", asset=asset_cfg.asset, t_start=win.t_start)
            return wt
        return wt

    async def check_timeouts(self, now: float | None = None) -> None:
        """周期检查：PTB 兜底/结算超时（覆盖当前 + pending）。now 可注入（测试用）。"""
        now = time.time() if now is None else now
        for wt in list(self._windows.values()):
            await self._timeout_check(wt, now)
        for dq in self._pending.values():
            for wt in list(dq):
                await self._timeout_check(wt, now)

    async def _timeout_check(self, wt: WindowTwap, now: float) -> None:
        if wt.ptb is None and wt.self_result is None:
            # ① spot 缓冲重建兜底（仅当前窗口；pending 已过窗口期兜底无意义）
            if (self._windows.get(wt.asset) is wt
                    and now - wt.t_start >= PTB_FALLBACK_AT_S
                    and self._binance_twap is not None):
                value = self._binance_twap(wt.asset, wt.t_start, wt.lookback_s)
                if value is not None:
                    wt.ptb = value
                    wt.ptb_obs_ms = wt.t_start * 1000
                    wt.ptb_src = "spot_rebuilt"
                    self.log.info("PTB captured (spot rebuilt)", asset=wt.asset,
                                  t_start=wt.t_start, ptb=str(value))
                    return
            # ② 超时仍未捕获 → 窗口作废
            if now - wt.t_start > PTB_TIMEOUT_S:
                wt.ptb_src = "missing"
                wt.self_result = "UNKNOWN"
                wt.settled_at_ms = now_ms()
                await self._persist_settlement(wt)
                self.log.error("PTB timeout: window untradable", asset=wt.asset, t_start=wt.t_start)
        elif wt.final is None and wt.self_result is None and wt.ptb is not None:
            # ③ final spot 重建兜底（当前 + pending 均适用，先于 20s 超时）
            if now - wt.t_end >= FINAL_FALLBACK_AT_S and self._binance_twap is not None:
                value = self._binance_twap(wt.asset, wt.t_end, wt.lookback_s)
                if value is not None:
                    wt.final = value
                    wt.final_obs_ms = wt.t_end * 1000
                    wt.final_src = "spot_rebuilt"
                    self.log.info("final TWAP captured (spot rebuilt)", asset=wt.asset,
                                  t_start=wt.t_start, final=str(value))
                    await self._settle(wt)
                    return
            # ④ 超时仍缺失 → UNKNOWN
            if now - wt.t_end > FINAL_TIMEOUT_S:
                wt.self_result = "UNKNOWN"
                wt.settled_at_ms = now_ms()
                await self._persist_settlement(wt)
                self.log.error("final TWAP timeout", asset=wt.asset, t_start=wt.t_start)

    async def _settle(self, wt: WindowTwap) -> None:
        assert wt.ptb is not None and wt.final is not None
        # 结算裁决只认官方来源：PTB/final 必须都是 RTDS。
        # spot 重建值带 ±0.1% 噪声（5m 胜负常由 <0.05% 决定），混合或全 spot 来源
        # 一律搁置为 UNKNOWN，由 gamma 官方结果在 reconcile 阶段结算（回调）。
        if wt.ptb_src == "rtds" and wt.final_src == "rtds":
            wt.self_result = "UP" if wt.final >= wt.ptb else "DOWN"
        else:
            wt.self_result = "UNKNOWN"
            self.log.warning(
                "settlement withheld: non-official PTB/final sources, awaiting gamma",
                asset=wt.asset, t_start=wt.t_start, ptb_src=wt.ptb_src, final_src=wt.final_src,
            )
        wt.settled_at_ms = now_ms()
        await self._persist_settlement(wt)
        self.log.info(
            "self-settled",
            asset=wt.asset,
            t_start=wt.t_start,
            result=wt.self_result,
            ptb=str(wt.ptb),
            final=str(wt.final),
            ptb_obs_ms=wt.ptb_obs_ms,
            final_obs_ms=wt.final_obs_ms,
        )

    async def _persist_settlement(self, wt: WindowTwap) -> None:
        rec = await self.repo.get_market(asset=wt.asset, t_start=wt.t_start)
        if rec is None:
            self.log.warning("settlement without market record", asset=wt.asset, t_start=wt.t_start)
            return
        await self.repo.save_settlement(
            market_id=rec.id,
            ptb_e18=str(wt.ptb) if wt.ptb is not None else None,
            final_e18=str(wt.final) if wt.final is not None else None,
            ptb_obs_ts_ms=wt.ptb_obs_ms,
            final_obs_ts_ms=wt.final_obs_ms,
            self_result=wt.self_result,
            self_settled_at_ms=wt.settled_at_ms,
            ptb_src=wt.ptb_src,
        )

    # ── gamma 对账 ───────────────────────────────────────────
    async def reconcile_gamma(self, asset: str, rec) -> None:
        """rec: MarketRecord；用 gamma outcomePrices 与自结算对账。

        自结算被搁置/缺失（非官方 PTB 来源）时，gamma 官方结果是结算裁决的
        最终依据 → 通过 on_gamma_settle 回调完成仓位结算。
        """
        prices = rec.gamma_outcome_prices
        if not prices or rec.gamma_closed is not True:
            return
        gamma_result = "UP" if prices[0] == "1" else "DOWN"
        wt = self._find(asset, rec.t_start)
        if wt is None:
            # 跨进程场景：内存无 WindowTwap → 从 DB 读自结算结果对账
            s = await self.repo.get_settlement(rec.market_id)
            if s is None:
                return
            if s.self_result not in ("UP", "DOWN"):
                # 自结算缺失/搁置 → gamma 结账
                if self._on_gamma_settle is not None:
                    await self._on_gamma_settle(rec.market_id, gamma_result)
                await self.repo.mark_settlement_reconciled(
                    rec.market_id, gamma_result=gamma_result, gamma_prices=",".join(prices),
                    dispute=None,
                )
                self.log.info("settlement resolved by gamma (db)", asset=asset, t_start=rec.t_start,
                              gamma_result=gamma_result)
                return
            dispute = None if s.self_result == gamma_result else (
                f"self={s.self_result} gamma={gamma_result} ptb={s.ptb_e18} final={s.final_e18}"
            )
            if dispute:
                self.log.error("SETTLEMENT DISPUTE (db)", asset=asset, t_start=rec.t_start, dispute=dispute)
            else:
                self.log.info("settlement reconciled (db)", asset=asset, t_start=rec.t_start,
                              self_result=s.self_result, gamma_result=gamma_result)
            await self.repo.mark_settlement_reconciled(
                rec.market_id, gamma_result=gamma_result, gamma_prices=",".join(prices), dispute=dispute,
            )
            return
        wt.gamma_result = gamma_result
        wt.reconciled = True
        if wt.self_result not in ("UP", "DOWN"):
            # 自结算被搁置/缺失 → gamma 结账（仓位结算由回调完成）
            if self._on_gamma_settle is not None:
                await self._on_gamma_settle(rec.market_id, gamma_result)
            self.log.info("settlement resolved by gamma", asset=asset, t_start=rec.t_start,
                          gamma_result=gamma_result)
        elif wt.self_result != gamma_result:
            wt.dispute = f"self={wt.self_result} gamma={gamma_result} ptb={wt.ptb} final={wt.final}"
            self.log.error("SETTLEMENT DISPUTE", asset=asset, t_start=rec.t_start, dispute=wt.dispute)
        else:
            self.log.info("settlement reconciled", asset=asset, t_start=rec.t_start,
                          self_result=wt.self_result, gamma_result=gamma_result)
        await self.repo.mark_settlement_reconciled(
            rec.market_id,
            gamma_result=gamma_result,
            gamma_prices=",".join(prices),
            dispute=wt.dispute,
        )

    # ── 查询 ─────────────────────────────────────────────────
    def current(self, asset: str) -> WindowTwap | None:
        return self._windows.get(asset)

    def find(self, asset: str, t_start: int) -> WindowTwap | None:
        return self._find(asset, t_start)

    def latest_sample(self, symbol: str, window_s: int) -> tuple[int, Decimal] | None:
        return self._latest.get((symbol, window_s))

    def ptb_ready(self, asset: str) -> bool:
        wt = self._windows.get(asset)
        return wt is not None and wt.ptb is not None and wt.self_result is None

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for a, w in self._windows.items():
            out[a] = {
                "t_start": w.t_start,
                "t_end": w.t_end,
                "lookback_s": w.lookback_s,
                "ptb": str(w.ptb) if w.ptb is not None else None,
                "ptb_src": w.ptb_src,
                "final": str(w.final) if w.final is not None else None,
                "self_result": w.self_result,
                "reconciled": w.reconciled,
                "n_samples": len(w.samples),
            }
        for a, dq in self._pending.items():
            for w in dq:
                key = f"{a}#{w.t_start}"
                out[key] = {
                    "t_start": w.t_start,
                    "t_end": w.t_end,
                    "lookback_s": w.lookback_s,
                    "ptb": str(w.ptb) if w.ptb is not None else None,
                    "final": str(w.final) if w.final is not None else None,
                    "self_result": w.self_result,
                    "reconciled": w.reconciled,
                }
        return out
