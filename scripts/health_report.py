"""策略与指标全面体检：逻辑状态 + 核心指标 + 近期活动。"""
import json
import sqlite3
import datetime

c = sqlite3.connect("data/pm5hft.db")

print("== 1. 进程与数据新鲜度 ==")
import urllib.request

d = json.loads(urllib.request.urlopen("http://127.0.0.1:8090/api/status", timeout=5).read())
print(f"  面板: {len(d.get('windows',[]))} 资产在线 | fresh_age={d.get('fresh_age')}s")
print(f"  权益: {d.get('equity')}")

print("\n== 2. 核心指标 ==")
buys = c.execute("SELECT COUNT(*) FROM fills WHERE side='BUY'").fetchone()[0]
sells = c.execute("SELECT COUNT(*) FROM fills WHERE side='SELL'").fetchone()[0]
pnl = c.execute("SELECT SUM(CAST(realized_pnl AS REAL)) FROM positions").fetchone()[0]
wins = c.execute("SELECT COUNT(*) FROM positions WHERE CAST(realized_pnl AS REAL)>0").fetchone()[0]
losses = c.execute("SELECT COUNT(*) FROM positions WHERE CAST(realized_pnl AS REAL)<0").fetchone()[0]
t0 = c.execute("SELECT MIN(ts_ms) FROM fills").fetchone()[0]
t1 = c.execute("SELECT MAX(ts_ms) FROM fills").fetchone()[0]
hours = (t1 - t0) / 3.6e6 if t0 and t1 else 0
print(f"  运行 {hours:.1f} 小时 | 买入 {buys} | 卖出 {sells}")
print(f"  PnL {pnl:+.4f} | 盈利 {wins} 笔 | 亏损 {losses} 笔 | 亏损率 {losses/max(1,buys):.1%}")
print(f"  成交速率 {buys/hours:.1f} 笔/小时")

print("\n== 3. 分资产 ==")
for a, n, p in c.execute(
        "SELECT m.asset, COUNT(*), SUM(CAST(p.realized_pnl AS REAL)) FROM positions p "
        "JOIN markets m ON m.id=p.market_id GROUP BY m.asset ORDER BY 3 DESC"):
    print(f"  {a:6} {n:3d} 笔  {p:+.4f}")

print("\n== 4. 终态分布 ==")
for st, n in c.execute(
        "SELECT state, COUNT(*) FROM positions GROUP BY state"):
    print(f"  {st}: {n}")

print("\n== 5. 结算与对账 ==")
for st, n in c.execute(
        "SELECT self_result, COUNT(*) FROM settlements GROUP BY self_result"):
    print(f"  自结算 {st}: {n}")
recon = c.execute(
    "SELECT COUNT(*) FROM settlements WHERE self_result IN ('UP','DOWN') AND "
    "gamma_result IS NOT NULL AND self_result != gamma_result").fetchone()[0]
print(f"  官方对账不一致: {recon}")

print("\n== 6. 近期活动（最新5笔） ==")
for r in c.execute(
        "SELECT ts_ms, side, price, qty FROM fills ORDER BY id DESC LIMIT 5"):
    t = datetime.datetime.fromtimestamp(r[0] / 1000, datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"  {t} {r[1]} {r[2]} x{r[3]}")

print("\n== 7. 挂单质量 ==")
for st, n in c.execute(
        "SELECT state, COUNT(*) FROM orders GROUP BY state"):
    print(f"  {st}: {n}")
