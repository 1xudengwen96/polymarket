"""市场注册表：slug 推导 → Gamma 事件拉取 → token/条件 ID 注册 → 状态轮询与对账。

要点（docs/00）：
- clobTokenIds/outcomes/outcomePrices 是双重 JSON 字符串；
- 权威窗口时间 = slug t_start + duration_s（gamma startDate 不可信）；
- cryptoMarketConfig.twapLookbackSeconds 决定结算 TWAP 窗口；
- Gamma 限速：本地 1 rps 令牌桶 + 重试。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .clock import AssetWindow
from .config import AssetConfig, Config
from .logging_setup import get_logger


@dataclass
class MarketRecord:
    market_id: int
    event_id: int | None
    slug: str
    asset: str
    t_start: int
    t_end: int
    condition_id: str
    token_up: str
    token_down: str
    question: str | None = None
    resolution_source: str | None = None
    twap_lookback_s: int | None = None
    tick_size: str | None = None
    min_order_size: str | None = None
    neg_risk: bool | None = None
    fee_schedule: dict[str, Any] | None = None
    accepting_orders: bool | None = None
    gamma_closed: bool | None = None
    gamma_outcome_prices: list[str] | None = None
    last_refresh_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class MarketRegistry:
    def __init__(self, config: Config, repo) -> None:
        self.config = config
        self.repo = repo
        self.log = get_logger("market.registry")
        self._markets: dict[tuple[str, int], MarketRecord] = {}  # (asset, t_start) -> record
        self._gamma_last_ts = 0.0
        self._gamma_min_interval = 0.7  # ~1.4 rps 上限，留余量

    # ── 查询 ─────────────────────────────────────────────────
    def current_window(self, asset_cfg: AssetConfig, now_s: int | None = None) -> AssetWindow:
        now_s = int(time.time()) if now_s is None else now_s
        t_start = (now_s // asset_cfg.duration_s) * asset_cfg.duration_s
        return AssetWindow(asset=asset_cfg.asset, tf_label=asset_cfg.tf_label,
                           t_start=t_start, duration_s=asset_cfg.duration_s)

    def get(self, asset: str, t_start: int) -> MarketRecord | None:
        return self._markets.get((asset, t_start))

    def current_record(self, asset: str, asset_cfg: AssetConfig, now_s: int | None = None) -> MarketRecord | None:
        win = self.current_window(asset_cfg, now_s)
        return self.get(asset, win.t_start)

    def subscription_tokens(self) -> list[str]:
        """当前活跃窗口的全部 token（Market WS 订阅用）。"""
        out: list[str] = []
        for rec in self._markets.values():
            out.extend([rec.token_up, rec.token_down])
        return out

    def prune(self, cutoff_s: int) -> int:
        """移除 t_end < cutoff 的过期窗口（订阅列表保持精简，
        防列表过长被交易所订阅帧截断 → 尾部 token 收不到簿口）。"""
        doomed = [(a, t) for (a, t), r in self._markets.items() if r.t_end < cutoff_s]
        for key in doomed:
            self._markets.pop(key, None)
        return len(doomed)

    def windows_for(self, asset: str) -> list[MarketRecord]:
        """某资产的全部已注册窗口（时间升序）。"""
        recs = [r for (a, _), r in self._markets.items() if a == asset]
        return sorted(recs, key=lambda r: r.t_start)

    # ── Gamma 拉取 ───────────────────────────────────────────
    async def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._gamma_last_ts + self._gamma_min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._gamma_last_ts = time.monotonic()

    async def fetch_event(self, asset_cfg: AssetConfig, win: AssetWindow) -> dict[str, Any] | None:
        url = f"{asset_cfg.gamma_api}/events"
        params = {"slug": win.slug}
        headers = {"User-Agent": "pm5hft/0.1 (paper research bot)"}
        last_err: Exception | None = None
        for attempt in range(3):
            await self._throttle()
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                    async with session.get(url, params=params, headers=headers) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(2.0 * (attempt + 1))
                            continue
                        resp.raise_for_status()
                        data = await resp.json()
                        if isinstance(data, list) and data:
                            return data[0]
                        self.log.warning("gamma: event not found", slug=win.slug)
                        return None
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.log.warning("gamma fetch failed", slug=win.slug, attempt=attempt + 1, err=str(e)[:120])
                await asyncio.sleep(1.0 * (attempt + 1))
        self.log.error("gamma fetch failed permanently", slug=win.slug, err=str(last_err)[:200])
        return None

    @staticmethod
    def parse_event(event: dict[str, Any], win: AssetWindow) -> MarketRecord | None:
        markets = event.get("markets") or []
        if not markets:
            return None
        m = markets[0]

        def _json_str(s):
            if isinstance(s, str):
                try:
                    return json.loads(s)
                except json.JSONDecodeError:
                    return None
            return s

        tokens = _json_str(m.get("clobTokenIds")) or []
        if len(tokens) < 2:
            return None
        crypto_cfg = m.get("cryptoMarketConfig") or {}
        return MarketRecord(
            market_id=int(m["id"]),
            event_id=int(event.get("id")) if event.get("id") else None,
            slug=win.slug,
            asset=win.asset,
            t_start=win.t_start,
            t_end=win.t_end,
            condition_id=m.get("conditionId") or "",
            token_up=tokens[0],
            token_down=tokens[1],
            question=m.get("question"),
            resolution_source=m.get("resolutionSource"),
            twap_lookback_s=int(crypto_cfg.get("twapLookbackSeconds") or 0) or None,
            tick_size=str(m.get("orderPriceMinTickSize") or ""),
            min_order_size=str(m.get("orderMinSize") or ""),
            neg_risk=bool(m.get("negRisk")) if m.get("negRisk") is not None else None,
            fee_schedule=m.get("feeSchedule"),
            accepting_orders=bool(m.get("acceptingOrders")),
            gamma_closed=bool(m.get("closed")),
            gamma_outcome_prices=_json_str(m.get("outcomePrices")),
            last_refresh_ms=int(time.time() * 1000),
        )

    async def register_window(self, asset: str, asset_cfg: AssetConfig, win: AssetWindow) -> MarketRecord | None:
        """注册（或刷新）一个窗口。幂等。"""
        event = await self.fetch_event(asset_cfg, win)
        if event is None:
            return None
        rec = self.parse_event(event, win)
        if rec is None:
            self.log.warning("market parse failed", slug=win.slug)
            return None
        self._markets[(asset, win.t_start)] = rec
        await self.repo.upsert_market(rec)
        await self.repo.add_market_status(
            rec.market_id,
            "registered" if rec.gamma_closed else "trading",
            accepting_orders=rec.accepting_orders,
            gamma_closed=rec.gamma_closed,
            gamma_outcome_prices=rec.gamma_outcome_prices,
            detail=None,
        )
        self.log.info(
            "market registered",
            slug=win.slug,
            market_id=rec.market_id,
            twap_lookback_s=rec.twap_lookback_s,
            neg_risk=rec.neg_risk,
            tick=rec.tick_size,
            accepting=rec.accepting_orders,
        )
        return rec

    async def refresh(self, asset: str, asset_cfg: AssetConfig, rec: MarketRecord) -> None:
        """刷新已有窗口的 gamma 状态（accepting/closed/outcomePrices 对账）。"""
        win = AssetWindow(asset=asset, tf_label=asset_cfg.tf_label,
                          t_start=rec.t_start, duration_s=rec.t_end - rec.t_start)
        event = await self.fetch_event(asset_cfg, win)
        if event is None:
            return
        fresh = self.parse_event(event, win)
        if fresh is None:
            return
        changed = fresh.accepting_orders != rec.accepting_orders or fresh.gamma_closed != rec.gamma_closed
        prices_changed = fresh.gamma_outcome_prices != rec.gamma_outcome_prices
        rec.accepting_orders = fresh.accepting_orders
        rec.gamma_closed = fresh.gamma_closed
        rec.gamma_outcome_prices = fresh.gamma_outcome_prices
        rec.last_refresh_ms = int(time.time() * 1000)
        if changed:
            state = "settled" if fresh.gamma_closed else "trading"
            await self.repo.add_market_status(
                rec.market_id,
                state,
                accepting_orders=fresh.accepting_orders,
                gamma_closed=fresh.gamma_closed,
                gamma_outcome_prices=fresh.gamma_outcome_prices,
                detail="refresh",
            )
            self.log.info(
                "market state changed",
                slug=rec.slug,
                accepting=fresh.accepting_orders,
                closed=fresh.gamma_closed,
                prices=fresh.gamma_outcome_prices,
            )
        elif prices_changed:
            self.log.debug("outcomePrices updated", slug=rec.slug, prices=fresh.gamma_outcome_prices)
