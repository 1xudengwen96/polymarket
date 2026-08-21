#!/usr/bin/env python3
"""扫描跨平台"快速结算 + 高流动性"的独立市场候选（Kalshi + Polymarket）。

目标：为 pm5hft 找 10 个可与加密 5m 并行跑、且与加密同涨同跌无关的市场。
输出：按 结算距离/流动性 排序的候选表 + 独立性提示。

用法: python scripts/scan_portfolio_markets.py [--max-close-hours 48]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

UA = {"User-Agent": "Mozilla/5.0 (compatible; pm5hft-scan/0.1)"}
GAMMA = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def get(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def piso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def kalshi_classify(title: str, ticker: str) -> str | None:
    t = (title + " " + ticker).lower()
    if re.search(r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|doge)\b", t) and re.search(r"up|down|higher|lower|price", t):
        return "crypto"
    if re.search(r"highest temperature|temperature increase|rain|snowfall|precipitation|wind gust|weather", t) \
            and not re.search(r"win the|championship|match", t):
        return "weather"
    if re.search(r"\b(nfl|nba|mlb|nhl|college football|soccer|tennis|ufc|boxing|golf)\b", t) or re.search(r"win the .*match|win .*game", t):
        return "sports"
    if re.search(r"election|president|senate|fed|interest rate|inflation|cpi|unemployment|gdp", t):
        return "macro"
    return None


def kalshi_scan(max_close_hours: float) -> list[dict]:
    out, cursor = [], ""
    now = time.time()
    for _ in range(12):
        params = {"limit": 1000, "status": "open", "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        data = get(f"{KALSHI}/markets?" + urllib.parse.urlencode(params))
        rows = data.get("markets", [])
        if not rows:
            break
        for m in rows:
            ticker = m.get("ticker") or ""
            if m.get("mve_collection_ticker") or ticker.startswith("KXMV"):
                continue
            close = piso(m.get("close_time"))
            if close is None or close <= now or close - now > max_close_hours * 3600:
                continue
            cat = kalshi_classify(m.get("title") or "", ticker)
            if not cat:
                continue
            try:
                liq = float(m.get("liquidity_dollars") or 0)
            except (TypeError, ValueError):
                liq = 0.0
            try:
                vol = float(m.get("volume") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            out.append({
                "venue": "kalshi", "id": ticker, "cat": cat,
                "title": (m.get("title") or "")[:80],
                "mins_to_close": round((close - now) / 60, 1),
                "liq": round(liq, 0), "vol": round(vol, 0),
                "yes_ask": m.get("yes_ask_dollars"),
                "event": m.get("event_ticker"), "series": m.get("series_ticker"),
            })
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.1)
    return out


def poly_weather() -> list[dict]:
    now = time.time()
    out, off = [], 0
    while off < 1200:
        page = get(f"{GAMMA}/events?tag_slug=temperature&limit=200&offset={off}&closed=false")
        if not page:
            break
        for e in page:
            end = piso(e.get("endDate"))
            if end is None or end <= now:
                continue
            for m in (e.get("markets") or [])[:1]:
                try:
                    liq = float(m.get("liquidityNum") or 0)
                except (TypeError, ValueError):
                    liq = 0.0
                out.append({
                    "venue": "polymarket", "id": m.get("conditionId"), "cat": "weather",
                    "title": (e.get("title") or "")[:80],
                    "mins_to_close": round((end - now) / 60, 1),
                    "liq": round(liq, 0), "vol": round(float(m.get("volumeNum") or 0), 0),
                    "prices": m.get("outcomePrices"),
                })
        off += len(page)
        if len(page) < 200:
            break
        time.sleep(0.1)
    return out


def poly_sports(max_close_hours: float) -> list[dict]:
    now = time.time()
    out = []
    for tag in ("nba", "nfl", "mlb", "nhl", "ufc"):
        try:
            page = get(f"{GAMMA}/events?tag_slug={tag}&limit=200&closed=false")
        except Exception:
            continue
        for e in page:
            end = piso(e.get("endDate"))
            if end is None or end <= now or end - now > max_close_hours * 3600:
                continue
            for m in (e.get("markets") or [])[:1]:
                try:
                    liq = float(m.get("liquidityNum") or 0)
                except (TypeError, ValueError):
                    liq = 0.0
                out.append({
                    "venue": "polymarket", "id": m.get("conditionId"), "cat": "sports",
                    "title": (e.get("title") or "")[:80],
                    "mins_to_close": round((end - now) / 60, 1),
                    "liq": round(liq, 0), "vol": round(float(m.get("volumeNum") or 0), 0),
                })
        time.sleep(0.1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-close-hours", type=float, default=48.0)
    args = ap.parse_args()

    print("== 扫描中（Kalshi 全市场 → Polymarket 天气/体育）… ==", flush=True)
    k = kalshi_scan(args.max_close_hours)
    print(f"Kalshi 开盘中且 {args.max_close_hours}h 内结算的候选: {len(k)}")
    w = poly_weather()
    print(f"Polymarket 天气（未来结算）: {len(w)}")
    s = poly_sports(12)
    print(f"Polymarket 体育（12h 内结算）: {len(s)}")

    all_rows = k + w + s

    print("\n== 按类别查看：结算快 + 流动性排名 ==")
    for cat in ("weather", "sports", "crypto", "macro"):
        rows = [r for r in all_rows if r["cat"] == cat]
        rows.sort(key=lambda r: (-r["liq"], r["mins_to_close"]))
        print(f"\n--- {cat} (n={len(rows)}) ---")
        for r in rows[:15]:
            print(f"  {r['venue']:<11} {r['mins_to_close']:>8.1f}min | liq={r['liq']:>10.0f} | vol={r['vol']:>10.0f} | {r['title']}")

    # 独立候选：天气不同城市 / 体育不同场次
    print("\n== 独立候选（weather 按城市去重 / sports 按标题去重）==")
    seen: set[str] = set()
    for r in sorted(all_rows, key=lambda r: (-r["liq"], r["mins_to_close"])):
        key = r["title"]
        if key in seen:
            continue
        seen.add(key)
        print(f"  {r['venue']:<11} {r['mins_to_close']:>8.1f}min | liq={r['liq']:>10.0f} | [{r['cat']:<7}] {r['title']}")


if __name__ == "__main__":
    sys.exit(main())
