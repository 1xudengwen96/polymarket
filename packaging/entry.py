"""PyInstaller 入口：单 exe 同时支持机器人 + 监控面板。"""
import sys

# Windows 控制台默认 GBK，强制 UTF-8 避免中文日志乱码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pm5hft.main import cli

if __name__ == "__main__":
    sys.exit(cli())
