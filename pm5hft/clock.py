"""窗口时钟与 slug 推导。

权威时间来源（docs/00 §1）：slug 的 t_start + duration_s。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# 北京时间 = UTC+8（交易日程/止盈按北京日与北京小时计算）
BEIJING_TZ = timezone(timedelta(hours=8))


def now_ms() -> int:
    return int(time.time() * 1000)


def now_s() -> int:
    return int(time.time())


def beijing_now(ts_s: int | None = None) -> datetime:
    """北京时间（UTC+8）datetime；ts_s 缺省用当前 unix 秒。"""
    return datetime.fromtimestamp(now_s() if ts_s is None else ts_s, tz=BEIJING_TZ)


def beijing_hour(ts_s: int | None = None) -> int:
    """当前/指定时刻的北京小时（0-23）。"""
    return beijing_now(ts_s).hour


def beijing_date(ts_s: int | None = None) -> str:
    """当前/指定时刻的北京日期 YYYY-MM-DD（北京 0 点换日）。"""
    return beijing_now(ts_s).strftime("%Y-%m-%d")


def in_trading_window(ts_s: int | None, start_hour: int, end_hour: int) -> bool:
    """北京时间交易时段判定（小时粒度，[start, end)）。

    - start_hour == end_hour → 全天交易（关闭时段限制）；
    - start_hour < end_hour   → 普通区间，如 9→21 为 09:00-20:59；
    - start_hour > end_hour   → 跨夜区间，如 22→6 为 22:00-05:59。
    """
    if start_hour == end_hour:
        return True
    h = beijing_hour(ts_s)
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


def window_start(ts_s: int, duration_s: int) -> int:
    """UTC 向下取整到窗口起点。"""
    return (ts_s // duration_s) * duration_s


def build_slug(asset: str, tf_label: str, t_start: int) -> str:
    return f"{asset}-updown-{tf_label}-{t_start}"


@dataclass(frozen=True)
class AssetWindow:
    """一个资产的一个交易窗口。"""

    asset: str
    tf_label: str
    t_start: int
    duration_s: int

    @property
    def t_end(self) -> int:
        return self.t_start + self.duration_s

    @property
    def slug(self) -> str:
        return build_slug(self.asset, self.tf_label, self.t_start)

    def remaining_s(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return self.t_end - now

    def into_s(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return now - self.t_start
