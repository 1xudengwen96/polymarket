#!/usr/bin/env python3
"""扫描 Gamma 上"即将结算"的市场：按结算速度/时长/类别分组，找出非加密的快速结算市场。

用法: python scripts/scan_fast_markets.py [--max-markets 1500]
输出: 当前未关闭市场中，距结算最近的 TOP 列表（按类别分桶）+ 时长分布。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from collections import Counter, defaultdict

UA = {"User-Agent": "Mozilla/5.0 (compatible; pm5hft-scan/0.1)", "Accept": "application/json"}
GAMMA = "https://gamma-api.polymarket.com"


def get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def bucket(question: str, slug: str) -> str:
    q = (question or "").lower()
    s = (slug or "").lower()
    if re.search(r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|doge|bnb|hype|updown|crypto|altcoin|coinbase|binance)\b", q + " " + s):
        return "crypto"
    if re.search(r"\b(nba|nfl|mlb|nhl|soccer|football|tennis|f1|ufc|boxing|mma|golf|game|match|ncaa|super bowl|world cup|wimbledon|aus open|us open|french open)\b", q):
        return "sports"
    if re.search(r"\b(election|president|trump|biden|senate|congress|governor|mayor|prime minister|vote|referendum|leader|minister|presidency)\b", q):
        return "politics"
    if re.search(r"\b(temperature|degrees|rain|snow|weather|storm|hurricane|wind|precipitation)\b", q):
        return "weather"
    if re.search(r"\b(inflation|cpi|fed|interest rate|rate cut|gdp|unemployment|jobs report|recession|tariff|bitcoin etf|etf approval)\b", q):
        return "economics"
    if re.search(r"\b(oscar|grammy|emmy|album|movie|box office|billboard|song|film|actor)\b", q):
        return "entertainment"
    if re.search(r"\b(war|invasion|ceasefire|hostage|prisoner|peace|attack|missile|nuclear)\b", q):
        return "geopolitics"
    return "other"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=1500)
    ap.add_argument("--min-liquidity", type=float, default=0.0)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    now_ts = time.time()
    markets: list[dict] = []
    offset = 0
    while len(markets) < args.max_markets:
        page = get(f"{GAMMA}/markets?limit=500&offset={offset}&closed=false")
        if not page:
            break
        markets.extend(page)
        offset += len(page)
        if len(page) < 500:
            break
        time.sleep(0.15)

    def parse_iso(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    rows = []
    for m in markets:
        end = parse_iso(m.get("endDate"))
        start = parse_iso(m.get("startDate"))
        if end is None:
            continue
        mins_to_end = (end - now_ts) / 60
        if mins_to_end < 0:
            continue  # 已过 endDate 但 gamma 未标记 closed
        duration_h = (end - start) / 3600 if start else None
        try:
            liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        except (TypeError, ValueError):
            liq = 0.0
        try:
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        cat = bucket(m.get("question", ""), m.get("slug", ""))
        rows.append({
            "id": m.get("id"), "question": (m.get("question") or "")[:90],
            "slug": (m.get("slug") or "")[:70], "cat": cat,
            "mins_to_end": round(mins_to_end, 1),
            "duration_h": round(duration_h, 3) if duration_h is not None else None,
            "liq": liq, "vol": vol,
        })

    print(f"== 扫描 {len(markets)} 个未关闭市场（{now:%Y-%m-%d %H:%M} UTC）==")
    print(f"类别分布: {dict(Counter(r['cat'] for r in rows))}")

    print("\n== 距结算最近 TOP 30（全部市场，含加密货币）==")
    for r in sorted(rows, key=lambda x: x["mins_to_end"])[:30]:
        print(f"  {r['mins_to_end']:>7.1f} min | {r['cat']:<6} | liq={r['liq']:>9.0f} | {r['question']}")

    noncrypto = [r for r in rows if r["cat"] != "crypto"]
    fast = [r for r in noncrypto if r["mins_to_end"] <= 180]
    print(f"\n== 非加密且 3 小时内结算: {len(fast)} 个 ==")
    for r in sorted(fast, key=lambda x: x["mins_to_end"])[:40]:
        print(f"  {r['mins_to_end']:>7.1f} min | dur={r['duration_h']}h | liq={r['liq']:>9.0f} | vol={r['vol']:>10.0f} | {r['question']}")

    # 按类别看最短结算速度（无 crypto 作为参照）
    print("\n== 各类别最短时长（endDate-startDate）==")
    by_cat: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["duration_h"] is not None:
            by_cat[r["cat"]].append(r["duration_h"])
    for cat, ds in sorted(by_cat.items()):
        ds.sort()
        print(f"  {cat:<12} n={len(ds):<5} min={ds[0]:.3f}h  p10={ds[min(9, len(ds)-1)]:.3f}h  median={ds[len(ds)//2]:.3f}h")

    # 高流动性的快速结算市场（非加密）
    print("\n== 非加密 & 结算<12h & 流动性>=500 的市场 ==")
    cand = [r for r in noncrypto if r["duration_h"] is not None and r["duration_h"] <= 12 and r["liq"] >= args.min_liquidity]
    for r in sorted(cand, key=lambda x: x["duration_h"])[:30]:
        print(f"  dur={r['duration_h']:>6.3f}h | liq={r['liq']:>9.0f} | vol={r['vol']:>10.0f} | {r['cat']:<6} | {r['question']}")


if __name__ == "__main__":
    sys.exit(main())
