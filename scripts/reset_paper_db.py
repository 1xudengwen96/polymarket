"""清空纸面库的交易痕迹表（保留市场注册/TWAP/行情数据）。

用于 Paper Trading 验证开始前的干净起点。
用法: python scripts/reset_paper_db.py [--db data/pm5hft.db]
"""
import argparse
import sqlite3

TRADE_TABLES = ["orders", "fills", "positions", "decision_log", "settlements",
                "equity_snapshot", "strategy_stats_daily", "calibration_buckets"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/pm5hft.db")
    args = p.parse_args()
    conn = sqlite3.connect(args.db, timeout=10)
    for t in TRADE_TABLES:
        try:
            n = conn.execute(f"DELETE FROM {t}").rowcount
            print(f"  {t}: deleted {n}")
        except sqlite3.OperationalError as e:
            print(f"  {t}: skip ({e})")
    conn.commit()
    keep = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN "
        f"({','.join('?' * len(TRADE_TABLES))})"
        , TRADE_TABLES).fetchall()
    print("kept tables:", [r[0] for r in keep])
    conn.close()


if __name__ == "__main__":
    main()
