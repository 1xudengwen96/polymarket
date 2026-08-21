"""structlog 初始化 + 脱敏处理。"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import structlog

# 64-hex（私钥）/API key 形态脱敏
_HEX64 = re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b")
_KEYLIKE = re.compile(r"(api[_-]?key|secret|passphrase|private[_-]?key)(['\"]?\s*[:=]\s*['\"]?)([^,\s'\"]+)", re.I)


def _redact(_, __, event_dict):
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            value = _HEX64.sub(lambda m: f"{m.group(0)[:8]}…[REDACTED]", value)
            value = _KEYLIKE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
            event_dict[key] = value
    return event_dict


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    logging.basicConfig(stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")

    handlers: list = [structlog.stdlib.ProcessorFormatter.wrap_for_formatter]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "pm5hft.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        handlers.append(fh)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)
