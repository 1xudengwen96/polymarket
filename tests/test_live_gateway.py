"""LiveGateway 测试（fake SDK client 注入）。"""

import os
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pm5hft.config import Config
from pm5hft.execution.engine import FillEvent, OrderIntent
from pm5hft.execution.live import LiveGateway, _persist_api_credentials


class FakeAccepted:
    ok = True
    order_id = "clob-1"
    status = "live"
    trade_ids: list = []


class FakeRejected:
    ok = False
    code = "BAD"
    message = "rejected for test"


class FakeClient:
    def __init__(self) -> None:
        self.limit_calls: list[dict] = []
        self.market_calls: list[dict] = []
        self.cancelled: list[str] = []
        self.balance = "92425720"  # SDK 原始单位（92.425720 USDC × 1e6）
        self.reject_next = False

    async def place_limit_order(self, **kw):  # noqa: ANN003
        self.limit_calls.append(kw)
        if self.reject_next:
            return FakeRejected()
        return FakeAccepted()

    async def place_market_order(self, **kw):  # noqa: ANN003
        self.market_calls.append(kw)
        if self.reject_next:
            return FakeRejected()
        return FakeAccepted()

    async def cancel_order(self, order_id):  # noqa: ANN001
        self.cancelled.append(order_id)
        return True

    async def cancel_all(self) -> None:
        self.cancelled.append("ALL")

    async def get_balance_allowance(self, asset_type):  # noqa: ANN001
        return SimpleNamespace(balance=self.balance)

    async def close(self) -> None:
        pass

    async def subscribe(self, spec):  # noqa: ANN001
        raise NotImplementedError


def test_persist_api_credentials_updates_dotenv_in_place(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PM5HFT_MODE=live\n"
        "POLYMARKET_API_KEY=\n"
        "POLYMARKET_API_SECRET=old\n"
        "# keep this comment\n",
        encoding="utf-8",
    )
    inode = env_path.stat().st_ino
    credentials = SimpleNamespace(key="new-key", secret="new-secret", passphrase="new-pass")

    _persist_api_credentials(env_path, credentials)

    assert env_path.stat().st_ino == inode
    assert env_path.read_text(encoding="utf-8") == (
        "PM5HFT_MODE=live\n"
        "POLYMARKET_API_KEY=new-key\n"
        "POLYMARKET_API_SECRET=new-secret\n"
        "# keep this comment\n"
        "POLYMARKET_API_PASSPHRASE=new-pass\n"
    )


def make_gateway() -> tuple[LiveGateway, FakeClient]:
    cfg = Config()
    cfg.settings.mode = "live"
    cfg.settings.live = True
    cfg.live.allow_live = True
    os.environ["POLYMARKET_PRIVATE_KEY"] = "0xtest"

    fake = FakeClient()

    async def factory(pk, funder):  # noqa: ANN001
        return fake

    gw = LiveGateway(cfg, client_factory=factory)
    gw._start_user_stream = lambda: None  # 测试不启动后台流
    return gw, fake


def intent(**kw):  # noqa: ANN003
    base = dict(market_id=1, token_id="T1", side="BUY", price=Decimal("0.981"),
                qty=Decimal("5"), tif="GTD", post_only=True,
                client_order_id="cid-1", expires_at_ms=None)
    base.update(kw)
    return OrderIntent(**base)


async def test_live_submit_limit_is_gtc_post_only():
    gw, fake = make_gateway()
    state = await gw.submit(intent())
    assert state == "LIVE"  # SDK 小写 "live" 归一化为大写（maintain/OPEN_ORDER_STATES 依赖）
    call = fake.limit_calls[0]
    assert call["post_only"] is True
    assert call["expiration"] is None  # 交易所侧一律 GTC（本地截止撤单）
    assert call["price"] == Decimal("0.981")
    assert gw._rev_clob["clob-1"] == "cid-1"
    assert gw.clob_id_for("cid-1") == "clob-1"


async def test_live_submit_fak_sell_with_min_price():
    gw, fake = make_gateway()
    await gw.submit(intent(tif="FAK", side="SELL", price=Decimal("0.99")))
    call = fake.market_calls[0]
    assert call["order_type"] == "FAK"
    assert call["shares"] == Decimal("5")
    assert call["min_price"] == Decimal("0.99")
    assert call["max_price"] is None


async def test_live_rejection_raises():
    gw, fake = make_gateway()
    fake.reject_next = True
    with pytest.raises(ValueError, match="rejected"):
        await gw.submit(intent())


async def test_live_cancel_uses_clob_id():
    gw, fake = make_gateway()
    await gw.submit(intent())
    ok = await gw.cancel("cid-1")
    assert ok is True
    assert fake.cancelled == ["clob-1"]


async def test_live_equity_from_balance():
    gw, fake = make_gateway()
    eq = await gw.get_equity()
    assert eq == Decimal("92.425720")  # 原始单位 ÷ 1e6


async def test_live_trade_event_to_fill():
    gw, fake = make_gateway()
    await gw.submit(intent())
    fills: list[FillEvent] = []

    async def handler(fill):
        fills.append(fill)

    gw.fill_handler = handler
    payload = SimpleNamespace(
        id="trade-1", taker_order_id="clob-1", token_id="T1", side="BUY",
        price="0.981", size="5", fee_rate_bps="1", timestamp=1_000_000,
    )
    await gw._on_user_event(SimpleNamespace(type="trade", payload=payload))
    assert len(fills) == 1
    f = fills[0]
    assert f.order_id == "cid-1" and f.market_id == 0
    assert f.price == Decimal("0.981") and f.qty == Decimal("5")
    assert f.fee == Decimal("0.0005")  # 0.981*5*1bps
    assert f.src == "live"
    # 同 trade id 重发 → 去重
    await gw._on_user_event(SimpleNamespace(type="trade", payload=payload))
    assert len(fills) == 1


async def test_live_maker_fill_attribution():
    """post-only 挂单成交时我们是 MAKER：单号在 maker_orders 里，
    taker_order_id 是对手方——必须能从 maker_orders 归属并取我们的价格/数量。"""
    gw, fake = make_gateway()
    await gw.submit(intent())
    fills: list[FillEvent] = []

    async def handler(fill):
        fills.append(fill)

    gw.fill_handler = handler
    maker_order = SimpleNamespace(
        order_id="clob-1", matched_amount="5", price="0.98", side="BUY", fee_rate_bps="0",
    )
    payload = SimpleNamespace(
        id="trade-2", taker_order_id="someone-else", token_id="T1", side="SELL",
        price="0.98", size="5", fee_rate_bps="1", timestamp=2_000_000,
        trader_side="MAKER", maker_orders=[maker_order],
    )
    await gw._on_user_event(SimpleNamespace(type="trade", payload=payload))
    assert len(fills) == 1
    f = fills[0]
    assert f.order_id == "cid-1"  # 从 maker_orders 归属，不是 taker_order_id
    assert f.side == "BUY"  # 我们的方向（maker 侧）
    assert f.price == Decimal("0.98") and f.qty == Decimal("5")
    assert f.src == "live"


async def test_live_maker_fill_unknown_side_dropped():
    """完全陌生的成交（非我方订单）→ 不产生 FillEvent。"""
    gw, fake = make_gateway()
    fills: list[FillEvent] = []
    gw.fill_handler = lambda f: fills.append(f)  # noqa: ARG005
    payload = SimpleNamespace(
        id="trade-3", taker_order_id="stranger", token_id="T1", side="SELL",
        price="0.5", size="1", fee_rate_bps="1", timestamp=3_000_000,
        trader_side="TAKER", maker_orders=[],
    )
    await gw._on_user_event(SimpleNamespace(type="trade", payload=payload))
    assert fills == []


async def test_live_startup_safety():
    gw, fake = make_gateway()
    await gw.startup_safety()
    assert fake.cancelled == ["ALL"]


async def test_live_fak_no_fill_returns_expired():
    gw, fake = make_gateway()
    state = await gw.submit(intent(tif="FAK", side="SELL", price=Decimal("0.99")))
    # FAK 无成交（trade_ids 空）→ EXPIRED → 执行引擎补发 on_order_closed 复位 pending 锁
    assert state == "EXPIRED"


async def test_live_client_creation_retries():

    from pm5hft.config import Config
    from pm5hft.execution.live import LiveGateway

    cfg = Config()
    cfg.settings.mode = "live"
    cfg.settings.live = True
    cfg.live.allow_live = True
    os.environ["POLYMARKET_PRIVATE_KEY"] = "0xtest"
    calls = {"n": 0}

    async def flaky_factory(pk, funder):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("network blip")
        return FakeClient()

    gw = LiveGateway(cfg, client_factory=flaky_factory)
    gw._start_user_stream = lambda: None
    # 前两次失败后第三次成功（重试+退避）
    client = await gw._ensure_client()
    assert calls["n"] == 3
    assert client is not None


async def test_live_client_creation_fails_hard():
    from pm5hft.config import Config
    from pm5hft.execution.live import LiveGateway

    cfg = Config()
    cfg.settings.mode = "live"
    cfg.settings.live = True
    cfg.live.allow_live = True
    os.environ["POLYMARKET_PRIVATE_KEY"] = "0xtest"

    async def always_fail(pk, funder):  # noqa: ANN001
        raise RuntimeError("relayer down")

    gw = LiveGateway(cfg, client_factory=always_fail)
    gw._start_user_stream = lambda: None
    with pytest.raises(RuntimeError, match="retries"):
        await gw._ensure_client()
