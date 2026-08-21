"""pm5hft 入口。

默认 paper 模式；LIVE 需要 PM5HFT_LIVE=true + config/live.yaml allow_live:true + 私钥。

打包成单 exe 后：
  pm5hft.exe              启动机器人（默认 paper）
  pm5hft.exe dashboard    启动监控面板（网页 http://127.0.0.1:8090）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from .config import ROOT, Config
from .logging_setup import get_logger, setup_logging
from .supervisor import Supervisor


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pm5hft", description="Polymarket Crypto 5m Up/Down HFT bot (paper-first)")
    p.add_argument("--mode", choices=["paper", "live"], default=None, help="运行模式（默认取 PM5HFT_MODE / paper）")
    p.add_argument("--assets", default=None, help="逗号分隔资产列表（覆盖 PM5HFT_ASSETS）")
    p.add_argument("--log-level", default=None, help="日志级别")
    return p


def cli(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    # 单 exe 子命令：pm5hft.exe dashboard [--port ...] 启动监控面板
    if argv and argv[0] == "dashboard":
        from . import dashboard

        sys.exit(dashboard.main(argv[1:]))
    args = build_parser().parse_args(argv)

    # env 覆盖在 Config 内完成；这里把 CLI 参数补进去
    if args.assets:
        os.environ["PM5HFT_ASSETS"] = args.assets
    if args.mode:
        os.environ["PM5HFT_MODE"] = args.mode
    if args.log_level:
        os.environ["PM5HFT_LOG_LEVEL"] = args.log_level

    try:
        config = Config()
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] 配置错误: {e}", file=sys.stderr)
        return 2

    setup_logging(config.settings.log_level, log_dir=ROOT / "logs")
    log = get_logger("main")

    sup = Supervisor(config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sig(signum, frame):  # noqa: ANN001
        log.info("signal received", signum=signum)
        sup.request_stop()

    try:
        signal.signal(signal.SIGINT, _sig)
    except (ValueError, OSError):
        pass
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _sig)
        except (ValueError, OSError):
            pass

    try:
        loop.run_until_complete(sup.run())
    except KeyboardInterrupt:
        sup.request_stop()
        loop.run_until_complete(sup.shutdown())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(cli())
