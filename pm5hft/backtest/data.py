"""回测数据层：TWAP60 标签重建、gamma 对账校验、数据加载。

标签重建（docs/08 §1.3）：官方 Chainlink TWAP 无历史 API → 用 Binance 1s bars
自算 TWAP60(t_start)/TWAP60(t_end)，与 gamma 结算对账；不一致率必须 < 2%。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from ..logging_setup import get_logger


def twap_from_bars(
    bars: list[tuple[int, float]],
    boundary_ms: int,
    lookback_s: int = 60,
    max_run_gap_s: int = 15,
    max_stale_ms: int = 8000,
) -> Decimal | None:
    """1s bars 前向填充后的简单均值 TWAP（近似 Chainlink 时间加权）。

    bars: 升序 (ts_ms, price)；boundary_ms: 窗口边界。
    缺失秒用最近成交价前向填充；连续缺口或首尾缺失超过 max_run_gap_s → None。
    """
    lo = boundary_ms - lookback_s * 1000
    seg = [p for ts, p in bars if lo <= ts < boundary_ms]
    if not seg:
        return None
    if boundary_ms - bars[-1][0] > max_stale_ms:
        return None
    first_ts = next(ts for ts, _ in bars if lo <= ts < boundary_ms)
    head_missing = (first_ts - lo) // 1000
    if head_missing > max_run_gap_s:
        return None
    filled = [seg[0]] * int(head_missing)
    prev_ts = first_ts
    for ts, p in bars:
        if ts < lo or ts >= boundary_ms:
            continue
        gap = (ts - prev_ts) // 1000 - 1
        if gap > max_run_gap_s:
            return None
        if gap > 0:
            filled.extend([filled[-1]] * int(gap))
        filled.append(p)
        prev_ts = ts
    tail_missing = (boundary_ms - prev_ts) // 1000 - 1
    if tail_missing > max_run_gap_s:
        return None
    if tail_missing > 0:
        filled.extend([filled[-1]] * int(tail_missing))
    if len(filled) < lookback_s:
        return None
    return Decimal(str(sum(filled[:lookback_s]) / lookback_s))


def load_bars_conn(conn: sqlite3.Connection, ts_from: int, ts_to: int, asset: str = "btc") -> list[tuple[int, float]]:
    cur = conn.execute(
        "SELECT ts_ms, price FROM ticks WHERE asset=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
        (asset, ts_from, ts_to),
    )
    out = [(int(ts), float(p)) for ts, p in cur.fetchall()]
    return out


def load_trades_conn(conn: sqlite3.Connection, ts_us_from: int, ts_us_to: int) -> list[tuple[int, float]]:
    cur = conn.execute(
        "SELECT ts_us, price FROM agg_trades WHERE ts_us BETWEEN ? AND ? ORDER BY ts_us",
        (ts_us_from, ts_us_to),
    )
    return [(int(ts), float(p)) for ts, p in cur.fetchall()]


def twap_from_trades(
    trades: list[tuple[int, float]],
    boundary_us: int,
    lookback_s: int = 60,
    min_coverage: float = 0.8,
) -> Decimal | None:
    """逐笔时间加权 TWAP（最接近 Chainlink 方法论的近似）。

    trades: 升序 (ts_us, price)；窗口 [boundary-L, boundary]；
    每笔价格按其在窗口内存续的时间加权；覆盖不足 → None。
    """
    lo = boundary_us - lookback_s * 1_000_000
    # 窗口内成交（含窗口前最后一笔，用于覆盖窗口起点）
    import bisect

    idx = bisect.bisect_left([t[0] for t in trades], lo)
    if idx > 0:
        idx -= 1
    seg = [t for t in trades[idx:] if t[0] < boundary_us]
    if not seg:
        return None
    if boundary_us - seg[-1][0] > 15_000_000:  # 尾部落数据 >15s → 陈旧
        return None
    total_w = 0.0
    total_pw = 0.0
    prev_ts, prev_p = None, None
    for ts, p in seg:
        if prev_ts is not None:
            dt = min(ts, boundary_us) - max(prev_ts, lo)
            if dt > 0:
                total_pw += prev_p * dt
                total_w += dt
        prev_ts, prev_p = ts, p
    if prev_ts is not None:
        dt = boundary_us - max(prev_ts, lo)
        if dt > 0:
            total_pw += prev_p * dt
            total_w += dt
    if total_w < lookback_s * 1_000_000 * min_coverage:
        return None
    return Decimal(str(total_pw / total_w))


def build_twap_labels(db_path: str) -> dict[str, Any]:
    """为全部有 gamma 标签的窗口重建 TWAP 标签并写回 market_labels。

    结算体制（2026-08-07 切换，见 docs/tail-evidence-11d.md §0）：
    - 8月7日前 resolution_source = btc-usd（现货价结算）→ 不重建 TWAP 标签，
      清空该窗口的 twap_* 字段（平局过滤对其无意义）；
    - 8月7日起 resolution_source = btc-usd-twap-30s-streams → 按市场自身
      twap_lookback_s（=30s）重建。

    返回统计: {n, n_labeled, n_mismatch, mismatch_rate, disputes: [...]}
    """
    log = get_logger("backtest.labels")
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ml.market_id, ml.t_start, ml.gamma_result, m.twap_lookback_s "
        "FROM market_labels ml LEFT JOIN markets m ON m.id = ml.market_id "
        "WHERE ml.gamma_result IN ('UP','DOWN') ORDER BY ml.t_start"
    ).fetchall()
    n = len(rows)
    n_labeled = 0
    n_mismatch = 0
    n_spot_era = 0
    disputes: list[dict] = []
    for market_id, t_start, gamma, lookback in rows:
        if not lookback:  # 现货结算时代：TWAP 标签不适用，清空
            n_spot_era += 1
            conn.execute(
                "UPDATE market_labels SET twap_ptb=NULL, twap_final=NULL, twap_result=NULL, "
                "twap_margin_bps=NULL, mismatch=0 WHERE market_id=?",
                (market_id,),
            )
            continue
        dur_s = 300
        t_end = t_start + dur_s
        trades = load_trades_conn(conn, (t_start - 60) * 1_000_000, (t_end + 15) * 1_000_000)
        ptb = twap_from_trades(trades, t_start * 1_000_000, int(lookback))
        final = twap_from_trades(trades, t_end * 1_000_000, int(lookback))
        if ptb is None or final is None:
            continue
        n_labeled += 1
        twap_result = "UP" if final >= ptb else "DOWN"
        mismatch = twap_result != gamma
        margin_bps = (final - ptb) / ptb * Decimal("10000") if ptb else Decimal("0")
        if mismatch:
            n_mismatch += 1
            disputes.append({"market_id": market_id, "t_start": t_start,
                             "twap": twap_result, "gamma": gamma,
                             "ptb": str(ptb), "final": str(final),
                             "margin_bps": float(margin_bps)})
        conn.execute(
            "UPDATE market_labels SET twap_ptb=?, twap_final=?, twap_result=?, twap_margin_bps=?, "
            "mismatch=? WHERE market_id=?",
            (str(ptb), str(final), twap_result, str(margin_bps), int(mismatch), market_id),
        )
    conn.commit()
    log.info("twap labels rebuilt (regime-aware)", n=n, n_labeled=n_labeled,
             n_spot_era=n_spot_era, n_mismatch=n_mismatch)
    # 过滤统计：|margin| < 1.5 bps 的窗口标签本身不确定（平局附近），交易时应排除
    near_ties = conn.execute(
        "SELECT COUNT(*) FROM market_labels WHERE twap_result IS NOT NULL "
        "AND ABS(CAST(twap_margin_bps AS REAL)) < 1.5"
    ).fetchone()[0]
    filtered_mismatch = conn.execute(
        "SELECT COUNT(*) FROM market_labels WHERE mismatch=1 "
        "AND ABS(CAST(twap_margin_bps AS REAL)) >= 1.5"
    ).fetchone()[0]
    filtered_n = n_labeled - near_ties
    conn.close()
    rate = n_mismatch / n_labeled if n_labeled else 0.0
    filtered_rate = filtered_mismatch / filtered_n if filtered_n else 0.0
    log.info("twap labels built", n=n, n_labeled=n_labeled, n_mismatch=n_mismatch,
             mismatch_rate=f"{rate:.2%}", near_tie_windows=near_ties,
             filtered_rate=f"{filtered_rate:.2%}")
    return {"n": n, "n_labeled": n_labeled, "n_mismatch": n_mismatch,
            "mismatch_rate": rate, "near_tie_windows": near_ties,
            "filtered_mismatch": filtered_mismatch, "filtered_rate": filtered_rate,
            "disputes": disputes}


def label_stats(db_path: str) -> dict[str, Any]:
    """从 market_labels 汇总标签质量统计（供报告）。"""
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM market_labels WHERE gamma_result IS NOT NULL").fetchone()[0]
    n_labeled = conn.execute("SELECT COUNT(*) FROM market_labels WHERE twap_result IS NOT NULL").fetchone()[0]
    n_mismatch = conn.execute("SELECT COUNT(*) FROM market_labels WHERE mismatch=1").fetchone()[0]
    near_ties = conn.execute(
        "SELECT COUNT(*) FROM market_labels WHERE twap_result IS NOT NULL "
        "AND ABS(CAST(twap_margin_bps AS REAL)) < 1.5"
    ).fetchone()[0]
    filtered_mm = conn.execute(
        "SELECT COUNT(*) FROM market_labels WHERE mismatch=1 "
        "AND ABS(CAST(twap_margin_bps AS REAL)) >= 1.5"
    ).fetchone()[0]
    conn.close()
    filtered_n = max(1, n_labeled - near_ties)
    return {
        "n": n,
        "n_labeled": n_labeled,
        "n_mismatch": n_mismatch,
        "mismatch_rate": n_mismatch / n_labeled if n_labeled else 0.0,
        "near_tie_windows": near_ties,
        "filtered_mismatch": filtered_mm,
        "filtered_rate": filtered_mm / filtered_n,
    }


def sensitivity_analysis(db_path: str, shifts_s: tuple[int, ...] = (-10, -5, 0, 5, 10)) -> dict[str, Any]:
    """标签对 TWAP 边界 ±5/10s 的敏感性：统计各偏移下与 gamma 的一致率。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT market_id, t_start, gamma_result FROM market_labels WHERE gamma_result IN ('UP','DOWN') "
        "ORDER BY t_start"
    ).fetchall()
    out: dict[str, Any] = {"windows": len(rows), "shifts": {}}
    for shift in shifts_s:
        agree = 0
        labeled = 0
        for _market_id, t_start, gamma in rows:
            t_end = t_start + 300
            trades = load_trades_conn(conn, (t_start - 60 + shift) * 1_000_000,
                                      (t_end + 15 + shift) * 1_000_000)
            ptb = twap_from_trades(trades, (t_start + shift) * 1_000_000, 60)
            final = twap_from_trades(trades, (t_end + shift) * 1_000_000, 60)
            if ptb is None or final is None:
                continue
            labeled += 1
            res = "UP" if final >= ptb else "DOWN"
            if res == gamma:
                agree += 1
        out["shifts"][shift] = {"labeled": labeled, "agree": agree,
                                "agree_rate": agree / labeled if labeled else 0.0}
    conn.close()
    return out
