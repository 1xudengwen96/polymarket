"""时钟与 slug 单元测试。"""

from pm5hft.clock import (
    AssetWindow,
    beijing_date,
    beijing_hour,
    build_slug,
    in_trading_window,
    window_start,
)


def test_window_start_floors_to_300():
    assert window_start(1786677150, 300) == 1786677000
    assert window_start(1786677000, 300) == 1786677000
    assert window_start(1786677299, 300) == 1786677000


def test_slug_format():
    assert build_slug("btc", "5m", 1786677000) == "btc-updown-5m-1786677000"


def test_asset_window_edges():
    w = AssetWindow(asset="btc", tf_label="5m", t_start=1786677000, duration_s=300)
    assert w.t_end == 1786677300
    assert w.slug == "btc-updown-5m-1786677000"
    assert w.remaining_s(1786677300) == 0.0
    assert w.into_s(1786677100) == 100.0


def test_real_market_slug_from_verified_data():
    # 2026-08-14 03:12:30Z UTC（ET 08-13 11:12:30PM）实测对应窗口
    t = 1786677150
    t0 = window_start(t, 300)
    assert t0 == 1786677000
    assert build_slug("btc", "5m", t0) == "btc-updown-5m-1786677000"


# 2026-08-14 03:12:30Z = 北京 2026-08-14 11:12:30（UTC+8）
_BJ_ANCHOR = 1786677150


def test_beijing_hour_and_date():
    assert beijing_hour(_BJ_ANCHOR) == 11
    assert beijing_date(_BJ_ANCHOR) == "2026-08-14"
    # 北京 0 点 = UTC 前一日 16:00 → 北京日期在 UTC 16:00 换日
    assert beijing_date(1786636799) == "2026-08-13"
    assert beijing_date(1786636800) == "2026-08-14"


def test_in_trading_window_normal():
    assert in_trading_window(_BJ_ANCHOR, 9, 21)        # 北京 11 时在 [9,21)
    assert in_trading_window(1786669200, 9, 21)        # 北京 09:00（含下界）
    assert not in_trading_window(1786712400, 9, 21)    # 北京 21:00（不含上界）
    assert not in_trading_window(1786716750, 9, 21)    # 北京 22 时


def test_in_trading_window_overnight_and_off():
    assert in_trading_window(1786716750, 22, 6)        # 北京 22 时在跨夜 [22,6)
    assert not in_trading_window(1786658400, 22, 6)    # 北京 06:00（不含上界）
    assert in_trading_window(_BJ_ANCHOR, 5, 5)         # start==end → 全天（时段关闭）
