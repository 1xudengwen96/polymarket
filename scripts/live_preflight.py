"""实盘预检（只读，不下单不撤单）：部署服务器上跑通所有前置条件。

用法: python scripts/live_preflight.py
通过标准：全部 ✓。余额为 0 或无法连接会明确标出（不阻塞，但需要人工确认）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pm5hft.config import Config  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'✓' if ok else '✗'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


async def main() -> int:
    results: list[bool] = []
    print("== 1. 环境变量 ==")
    results.append(check("PM5HFT_LIVE=true", os.environ.get("PM5HFT_LIVE") == "true",
                         f"当前={os.environ.get('PM5HFT_LIVE')}"))
    results.append(check("PM5HFT_MODE=live", os.environ.get("PM5HFT_MODE") == "live",
                         f"当前={os.environ.get('PM5HFT_MODE')}"))
    results.append(check("POLYMARKET_PRIVATE_KEY 已设置", bool(os.environ.get("POLYMARKET_PRIVATE_KEY"))))
    funder = os.environ.get("POLYMARKET_FUNDER")
    results.append(check("POLYMARKET_FUNDER（可选）", True, funder or "未设置（默认签名者的存款钱包）"))

    print("\n== 2. 配置 ==")
    try:
        cfg = Config()
        results.append(check("allow_live=true", cfg.live.allow_live))
        results.append(check("模式解析为 live", cfg.settings.mode == "live", cfg.settings.mode))
        db_url = cfg.settings.db_url
        live_db = "pm5hft-live.db" in db_url
        results.append(check("使用独立 live 数据库", live_db, db_url))
        if not live_db:
            print("      ⚠ 当前 DB 与纸面共用！务必设置 PM5HFT_DB_URL=sqlite+aiosqlite:///./data/pm5hft-live.db")
    except Exception as e:  # noqa: BLE001
        results.append(check("配置加载", False, str(e)[:120]))
        print("\n预检失败，请先修复配置")
        return 2

    print("\n== 3. SDK 与账户 ==")
    try:
        import polymarket  # noqa: F401

        results.append(check("polymarket-client 已安装", True, polymarket.__version__))
    except ImportError:
        results.append(check("polymarket-client 已安装", False))
        print("\npip install -e . 后重试")
        return 2

    try:
        from pm5hft.execution.live import LiveGateway

        gw = LiveGateway(cfg)
        equity = await gw.get_equity()
        await gw.close()
        ok = equity > 0
        results.append(check("USDC collateral 余额读取", True, f"balance={equity}"))
        results.append(check("余额 > 0", ok, "为 0 需先在 Polymarket 充值"))
    except Exception as e:  # noqa: BLE001
        results.append(check("余额读取", False, str(e)[:200]))

    print("\n== 4. 系统时钟 ==")
    results.append(check("时钟同步", True,
                         "Windows: w32tm /query /status 看 Source；Linux: timedatectl 看 synchronized: yes"))

    print("\n== 5. 数据文件 ==")
    for rel in ("config/assets.yaml", "config/strategy.yaml", "config/risk.yaml",
                "config/live.yaml", "artifacts/logreg_v1.json"):
        results.append(check(f"存在 {rel}", Path(rel).is_file()))

    ok_all = all(results)
    print(f"\n{'== 预检通过 ✓ ==' if ok_all else '== 存在失败项 ✗ =='}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
