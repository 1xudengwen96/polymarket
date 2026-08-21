"""执行层单元测试：幂等、限速、Paper 撮合规则、订单生命周期。"""

import asyncio
import os
import sys
from decimal import Decimal

import pytest
import pytest_asyncio

from pm5hft.features import BookState

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_uuid7_unique_and_ordered():
    from pm5hft.execution.engine import uuid7

    ids = [uuid7() for _ in range(10)]
    assert len(set(ids)) == 10
    assert all(len(x) == 32 for x in ids)


def test_deterministic_salt():
    from pm5hft.execution.engine import deterministic_salt

    assert deterministic_salt("abc") == deterministic_salt("abc")
    assert deterministic_salt("abc") != deterministic_salt("abd")


def test_token_bucket():
    from pm5hft.execution.engine import TokenBucket

    b = TokenBucket(rate_per_s=1.0, burst=2.0)
    assert b.try_take()
    assert b.try_take()
    assert not b.try_take()  # 爆发耗尽


@pytest_asyncio.fixture()
async def ctx():
    import tempfile

    from pm5hft.config import Config
    from pm5hft.db import close_db, create_schema, init_db
    from pm5hft.execution.paper import PaperGateway
    from pm5hft.features import FeatureStore
    from pm5hft.persistence import Repo

    cfg = Config()
    dbpath = os.path.join(tempfile.gettempdir(), f"pm5hft_test_{os.getpid()}.db")
    cfg.settings.db_url = f"sqlite+aiosqlite:///{dbpath}"
    init_db(cfg.settings.db_url)
    await create_schema()
    repo = Repo()
    store = FeatureStore(cfg)
    gw = PaperGateway(cfg, repo, store)
    yield cfg, repo, gw, store
    await close_db()
    try:
        os.remove(dbpath)
    except OSError:
        pass


class _FakeRec:
    market_id = 1
    token_up = "T_UP"
    token_down = "T_DOWN"
    asset = "btc"


@pytest.mark.asyncio
async def test_paper_maker_queue_and_partial_fill(ctx):
    cfg, repo, gw, store = ctx
    from pm5hft.execution.engine import OrderIntent

    # 真实簿：Up bid 0.42 x 100（我们挂 0.42 时排在 100 之后）
    book = store.books.setdefault("T_UP", BookState(token_id="T_UP"))
    book.update_snapshot([("0.42", "100"), ("0.41", "50")], [("0.43", "80")], "h1", 1)

    intent = OrderIntent(market_id=1, token_id="T_UP", side="BUY",
                         price=Decimal("0.42"), qty=Decimal("60"), tif="GTD", post_only=True)
    await repo.upsert_order(intent, mode="paper", salt="s")
    state = await gw.submit(intent)
    assert state == "LIVE"
    r = gw._resting[intent.client_order_id]
    assert r.ahead == Decimal("100")  # 排在真实 100 股之后

    # 成交 60@0.42：先消耗前面的 100，我方未成交
    await gw.on_trade("T_UP", Decimal("0.42"), Decimal("60"), "SELL", 1)
    assert gw._resting[intent.client_order_id].filled == Decimal("0")

    # 再成交 40@0.42：前面消耗完（60+40=100），我方仍未成交
    await gw.on_trade("T_UP", Decimal("0.42"), Decimal("40"), "SELL", 2)
    assert gw._resting[intent.client_order_id].filled == Decimal("0")

    # 再成交 30@0.42：前面 100 已耗尽，30 全部轮到我们
    await gw.on_trade("T_UP", Decimal("0.42"), Decimal("30"), "SELL", 3)
    assert gw._resting[intent.client_order_id].filled == Decimal("30")

    # 扫穿（0.40 < 0.42）：剩余 30 全部成交
    await gw.on_trade("T_UP", Decimal("0.40"), Decimal("5"), "SELL", 4)
    assert intent.client_order_id not in gw._resting


@pytest.mark.asyncio
async def test_paper_maker_no_cross_no_fill(ctx):
    cfg, repo, gw, store = ctx
    from pm5hft.execution.engine import OrderIntent

    book = store.books.setdefault("T_UP", BookState(token_id="T_UP"))
    book.update_snapshot([("0.42", "100")], [("0.43", "80")], "h1", 1)
    intent = OrderIntent(market_id=1, token_id="T_UP", side="BUY",
                         price=Decimal("0.42"), qty=Decimal("60"), tif="GTD", post_only=True)
    await repo.upsert_order(intent, mode="paper", salt="s")
    await gw.submit(intent)
    # 买方向成交（taker BUY）不触及我方买挂单
    await gw.on_trade("T_UP", Decimal("0.45"), Decimal("100"), "BUY", 1)
    assert gw._resting[intent.client_order_id].filled == Decimal("0")


@pytest.mark.asyncio
async def test_paper_post_only_rejects_cross(ctx):
    cfg, repo, gw, store = ctx
    from pm5hft.execution.engine import OrderIntent

    book = store.books.setdefault("T_UP", BookState(token_id="T_UP"))
    book.update_snapshot([("0.42", "100")], [("0.43", "80")], "h1", 1)
    intent = OrderIntent(market_id=1, token_id="T_UP", side="BUY",
                         price=Decimal("0.43"), qty=Decimal("60"), tif="GTD", post_only=True)
    await repo.upsert_order(intent, mode="paper", salt="s")
    state = await gw.submit(intent)
    assert state == "REJECTED"


@pytest.mark.asyncio
async def test_unfilled_taker_notifies_closed_once(ctx):
    """FAK 无流动性终结 → maintain 补发 on_order_closed（复位策略 pending 锁，防出场卡死）。"""
    cfg, repo, gw, store = ctx
    from pm5hft.execution.engine import ExecutionEngine, OrderIntent

    closed: list[tuple[str, str]] = []
    engine = ExecutionEngine(
        cfg, repo, gw,
        on_order_closed=lambda oid, mid, state: closed.append((oid, state)),
    )
    intent = OrderIntent(market_id=1, token_id="T_UP", side="BUY",
                         price=Decimal("0.43"), qty=Decimal("5"), tif="FAK", post_only=False)
    ok, _state = await engine.submit(intent)
    assert ok
    await asyncio.sleep(0.4)  # paper taker 250ms 延迟后异步标 EXPIRED（空簿无流动性）
    await engine.maintain()
    assert any(oid == intent.client_order_id and st == "EXPIRED" for oid, st in closed), closed
    closed.clear()
    await engine.maintain()  # 幂等：不重复通知
    assert not closed


@pytest.mark.asyncio
async def test_paper_taker_fok_depth_weighted(ctx):
    cfg, repo, gw, store = ctx
    from pm5hft.execution.engine import OrderIntent

    book = store.books.setdefault("T_UP", BookState(token_id="T_UP"))
    # asks: 0.43x100, 0.44x50
    book.update_snapshot([("0.42", "100")], [("0.43", "100"), ("0.44", "50")], "h1", 1)
    intent = OrderIntent(market_id=1, token_id="T_UP", side="BUY",
                         price=Decimal("0.44"), qty=Decimal("120"), tif="FOK", post_only=False)
    await repo.upsert_order(intent, mode="paper", salt="s")
    fills = []
    gw.fill_handler = lambda f: fills.append(f)
    state = await gw.submit(intent)
    assert state == "PENDING"
    # 等待延迟撮合
    await asyncio.sleep(0.35)
    assert len(fills) == 1
    f = fills[0]
    assert f.qty == Decimal("120")
    assert f.price == Decimal((0.43 * 100 + 0.44 * 20) / 120).quantize(Decimal("0.0001"))


@pytest.mark.asyncio
async def test_paper_taker_fok_insufficient_depth(ctx):
    cfg, repo, gw, store = ctx
    from pm5hft.execution.engine import OrderIntent

    book = store.books.setdefault("T_UP", BookState(token_id="T_UP"))
    book.update_snapshot([("0.42", "100")], [("0.43", "30")], "h1", 1)
    intent = OrderIntent(market_id=1, token_id="T_UP", side="BUY",
                         price=Decimal("0.43"), qty=Decimal("120"), tif="FOK", post_only=False)
    await repo.upsert_order(intent, mode="paper", salt="s")
    await gw.submit(intent)
    await asyncio.sleep(0.35)
    o = await repo.get_order(intent.client_order_id)
    assert o.state == "EXPIRED"


@pytest.mark.asyncio
async def test_execution_engine_lifecycle(ctx):
    cfg, repo, gw, store = ctx
    from pm5hft.execution.engine import ExecutionEngine, OrderIntent

    engine = ExecutionEngine(cfg, repo, gw, deadline_for=lambda mid: None)
    gw.fill_handler = engine.handle_fill
    intent = OrderIntent(market_id=1, token_id="T_UP", side="BUY",
                         price=Decimal("0.42"), qty=Decimal("60"), tif="GTD", post_only=True,
                         expires_at_ms=999999999999999)
    ok, state = await engine.submit(intent)
    assert ok and state == "LIVE"
    o = await repo.get_order(intent.client_order_id)
    assert o.state == "LIVE"
    assert o.salt is not None
    assert await engine.cancel(intent.client_order_id)
    o2 = await repo.get_order(intent.client_order_id)
    assert o2.state == "CANCELLED"


@pytest.mark.asyncio
async def test_execution_hard_deadline_blocks_new_orders(ctx):
    cfg, repo, gw, store = ctx
    from pm5hft.clock import now_ms
    from pm5hft.execution.engine import ExecutionEngine, OrderIntent

    engine = ExecutionEngine(cfg, repo, gw, deadline_for=lambda mid: now_ms() + 1000)  # 1s 后截止
    intent = OrderIntent(market_id=1, token_id="T_UP", side="BUY",
                         price=Decimal("0.42"), qty=Decimal("60"), tif="GTD", post_only=True)
    ok, reason = await engine.submit(intent)
    assert not ok
    assert "deadline" in reason
