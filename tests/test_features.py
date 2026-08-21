"""特征计算单元测试。"""

import pytest

from pm5hft.features import BookState, TickBuffer


def test_tick_buffer_returns():
    buf = TickBuffer()
    # 构造 70 个 1s bar，价格线性上涨 1%
    base = 60000.0
    for i in range(70):
        ts = 1_000_000_000 + i * 1000
        price = base * (1 + i / 10000.0)
        buf.on_trade(ts, price, 1.0, is_buyer_maker=(i % 2 == 0))
        buf.roll_to(ts + 500)
    r60 = buf.ret(60)
    assert r60 is not None
    assert abs(r60 - 0.006) < 0.002  # 60s ≈ 0.6%
    assert buf.ret(100) is None
    # 量能
    assert buf.vol(10) == pytest.approx(10.0)
    buys, sells = buf.agg_flow(10)
    assert buys + sells == pytest.approx(10.0)


def test_tick_buffer_twap_approx():
    buf = TickBuffer()
    for i in range(60):
        ts = 1_000_000_000 + i * 1000
        buf.on_trade(ts, 100.0 + i, 1.0, False)
        buf.roll_to(ts + 500)
    tw = buf.approx_twap(30)
    assert tw is not None
    # 最近 30 个 bar（i=30..59，价格 130..159）均值 = 144.5
    assert abs(tw - 144.5) < 0.001


def test_book_state_sorting_defensive():
    b = BookState(token_id="t")
    # 模拟 REST 实测的“从中间向外”乱序：bids 升序、asks 降序
    bids = [("0.01", "10"), ("0.50", "100"), ("0.51", "50")]
    asks = [("0.99", "9"), ("0.52", "80"), ("0.53", "70")]
    b.update_snapshot(bids, asks, "h1", 123)
    assert b.best_bid == 0.51
    assert b.best_ask == 0.52
    assert b.spread() == pytest.approx(0.01)
    assert b.obi(3) is not None
    # 价格变动：撤 0.51 档
    b.apply_price_change("0.51", "0", "BUY")
    assert b.best_bid == 0.50
    # 新增更低卖价
    b.apply_price_change("0.515", "25", "SELL")
    assert b.best_ask == 0.515


def test_empty_bars_forward_filled():
    """无成交的秒：价格前向填充，禁止 0 价进入价格序列。"""
    buf = TickBuffer()
    buf.on_trade(1_000_000_000, 100.0, 1.0, False)
    # 5 秒无成交
    buf.roll_to(1_000_000_000 + 5000)
    closes = buf.closes()
    assert all(c == 100.0 for c in closes), f"closes={closes}"
    assert buf.ret(3) == 0.0
    # 之后恢复成交
    buf.on_trade(1_000_000_000 + 6000, 110.0, 1.0, False)
    buf.roll_to(1_000_000_000 + 6500)
    assert buf.closes()[-1] == 110.0
    assert buf.ret(1) == pytest.approx(0.10)


def test_reversal_score_bounds():
    buf = TickBuffer()
    for i in range(30):
        ts = 1_000_000_000 + i * 1000
        # 价格先涨后跌回中位
        price = 100 + (i if i < 15 else 30 - i)
        buf.on_trade(ts, price, 1.0, False)
        buf.roll_to(ts + 500)
    s = buf.reversal_score(30)
    assert s is not None
    assert 0.0 <= s <= 1.0
