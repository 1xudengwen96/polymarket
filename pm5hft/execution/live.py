"""LiveGateway（docs/07 §7）：polymarket-client SDK 实盘网关。

- 双开关门控（PM5HFT_LIVE=true + config/live.yaml allow_live:true + 私钥）才可创建；
- 限价单：place_limit_order(post_only, GTC)——CLOB 要求 GTD ≥3 分钟，而本策略的
  GTD 寿命常 <3 分钟（末段入场），故交易所侧一律 GTC，窗口截止由执行引擎的
  本地 deadline 撤单负责（t_end-5s 全撤 + 启动 cancel_all 兜底）；
- FAK/FOK：place_market_order(shares + max/min_price 限价保护)；
- 成交：UserSpec 用户流 UserTradeEvent → FillEvent 回写（与 paper 同构）；
- 幂等：订单哈希由 SDK 签名；client_order_id ↔ CLOB order_id 双向映射。
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import deque
from decimal import Decimal
from pathlib import Path

from ..logging_setup import get_logger
from .engine import FillEvent, OrderIntent


_API_CREDENTIAL_ENV_NAMES = {
    "POLYMARKET_API_KEY": "key",
    "POLYMARKET_API_SECRET": "secret",
    "POLYMARKET_API_PASSPHRASE": "passphrase",
}


class CredentialPersistenceError(RuntimeError):
    """Derived credentials could not be made durable for the next restart."""


def _persist_api_credentials(path: Path, credentials) -> None:
    """Update the credential entries in a dotenv file without replacing its inode.

    Docker bind-mounts ``.env`` as a file, so an atomic rename cannot replace it.
    Rewriting the mounted file in place keeps the host copy and Compose env_file in
    sync for the next container recreation.
    """
    values = {
        env_name: str(getattr(credentials, attr_name))
        for env_name, attr_name in _API_CREDENTIAL_ENV_NAMES.items()
    }
    if not all(values.values()):
        raise RuntimeError("SDK returned incomplete Polymarket API credentials")

    with path.open("r+", encoding="utf-8") as env_file:
        try:
            import fcntl

            fcntl.flock(env_file.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Linux production path always has fcntl
            pass

        lines = env_file.readlines()
        found: set[str] = set()
        for index, line in enumerate(lines):
            for env_name, value in values.items():
                if re.match(rf"^(?:export\s+)?{re.escape(env_name)}\s*=", line):
                    lines[index] = f"{env_name}={value}\n"
                    found.add(env_name)
                    break
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        for env_name, value in values.items():
            if env_name not in found:
                lines.append(f"{env_name}={value}\n")

        env_file.seek(0)
        env_file.writelines(lines)
        env_file.truncate()
        env_file.flush()
        os.fsync(env_file.fileno())


class LiveGateway:
    def __init__(self, config, client_factory=None) -> None:
        if config.settings.mode != "live" or not config.settings.live or not config.live.allow_live:
            raise RuntimeError("LiveGateway requires LIVE mode with allow_live=true")
        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY missing")
        self.config = config
        self.log = get_logger("execution.live")
        self._client = None
        self._private_key = private_key
        self._funder = os.environ.get("POLYMARKET_FUNDER") or None
        self._api_key = os.environ.get("POLYMARKET_API_KEY") or None
        self._api_secret = os.environ.get("POLYMARKET_API_SECRET") or None
        self._api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE") or None
        provided = [self._api_key, self._api_secret, self._api_passphrase]
        if any(provided) and not all(provided):
            raise RuntimeError(
                "POLYMARKET_API_KEY, POLYMARKET_API_SECRET and "
                "POLYMARKET_API_PASSPHRASE must be provided together"
            )
        # client_order_id -> clob order_id；clob order_id -> client_order_id
        self._clob_ids: dict[str, str] = {}
        self._rev_clob: dict[str, str] = {}
        self.fill_handler = None  # ExecutionEngine.handle_fill 注入（与 paper 同构）
        self._user_task: asyncio.Task | None = None
        self._seen_trades: deque[str] = deque(maxlen=2000)  # 成交事件去重（流可能重发）
        self._client_factory = client_factory  # 测试注入

    # ── 客户端与用户流 ───────────────────────────────────────
    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                credential_source = "configured" if self._api_key else "derived"
                if self._client_factory is not None:
                    self._client = await self._client_factory(self._private_key, self._funder)
                else:
                    from polymarket import AsyncSecureClient
                    from polymarket.models.clob.api_key import ApiKeyCreds

                    credentials = None
                    if self._api_key:
                        credentials = ApiKeyCreds(
                            apiKey=self._api_key,
                            secret=self._api_secret,
                            passphrase=self._api_passphrase,
                        )
                    if credentials is not None:
                        # The credentials are provisioned in the environment. The
                        # SDK's default validation probes /auth/api-keys, which can
                        # time out behind the current network proxy; order signing
                        # still uses these credentials and wallet readiness remains
                        # checked by _ensure_wallet_ready().
                        self._client = await AsyncSecureClient._create(
                            private_key=self._private_key,
                            wallet=self._funder,
                            credentials=credentials,
                            validate_credentials=False,
                        )
                        # An explicit funder is an already-deployed wallet selected
                        # by the operator. Avoid the SDK's redundant relayer probe;
                        # startup still requires an authenticated collateral balance
                        # read before any strategy task is launched.
                        if self._funder is None:
                            self._client = await self._client._ensure_wallet_ready()
                    else:
                        credential_source = "derived"
                        self._client = await AsyncSecureClient.create(
                            private_key=self._private_key,
                            wallet=self._funder,
                        )
                        derived = self._client.credentials
                        if derived is None:
                            raise RuntimeError("SDK did not expose derived API credentials")
                        env_path = Path(os.environ.get("PM5HFT_ENV_FILE", ".env"))
                        try:
                            _persist_api_credentials(env_path, derived)
                        except Exception as e:  # noqa: BLE001
                            await self._client.close()
                            self._client = None
                            raise CredentialPersistenceError(
                                f"could not persist derived API credentials to {env_path}"
                            ) from e
                        self._api_key = str(derived.key)
                        self._api_secret = str(derived.secret)
                        self._api_passphrase = str(derived.passphrase)
                        self.log.info("derived API credentials persisted", path=str(env_path))
                self.log.info("live client created", funder=self._funder,
                              credentials=credential_source)
                self._start_user_stream()
                return self._client
            except CredentialPersistenceError:
                # Retrying would derive credentials repeatedly while the durable
                # storage problem remains unchanged.
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.log.warning("live client creation failed, retrying",
                                 attempt=attempt + 1, err=str(e)[:120])
                await asyncio.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"live client creation failed after retries: {last_err}")

    def _start_user_stream(self) -> None:
        async def _run() -> None:
            from polymarket.streams._specs import UserSpec

            while True:
                try:
                    handle = await self._client.subscribe(UserSpec(markets=None))
                    self.log.info("user stream connected")
                    async for event in handle:
                        await self._on_user_event(event)
                except Exception:
                    self.log.exception("user stream failed, reconnecting")
                    await asyncio.sleep(3.0)

        self._user_task = asyncio.create_task(_run())

    async def _on_user_event(self, event) -> None:
        ev_type = getattr(event, "type", None)
        if ev_type != "trade":
            return
        payload = getattr(event, "payload", None)
        if payload is None:
            return
        trade_id = str(getattr(payload, "id", "") or "")
        if trade_id and trade_id in self._seen_trades:
            return
        if trade_id:
            self._seen_trades.append(trade_id)
        # 归属：本策略挂单为 post-only，成交时是 MAKER——CLOB 用户流的
        # taker_order_id 是对手方单号，我们自己的单号在 maker_orders[].order_id。
        # 只查 taker_order_id 会把每一笔 maker 成交都当成陌生订单丢弃
        # （实盘首日 4 笔成交全部漏记的根因）。
        trader_side = str(getattr(payload, "trader_side", "") or "").upper()
        maker_orders = getattr(payload, "maker_orders", None) or []
        candidates = [str(getattr(payload, "taker_order_id", "") or "")]
        candidates += [str(getattr(mo, "order_id", "") or "") for mo in maker_orders]
        client_oid = next((self._rev_clob.get(o) for o in candidates if o and o in self._rev_clob), None)
        if client_oid is None:
            self.log.debug("trade for unknown order", clob=candidates)
            return
        if trader_side == "MAKER" and maker_orders:
            mo = next((m for m in maker_orders if str(getattr(m, "order_id", "") or "") in self._rev_clob), None)
            if mo is None:
                mo = maker_orders[0]
            price = Decimal(str(getattr(mo, "price", "0") or "0"))
            size = Decimal(str(getattr(mo, "matched_amount", "0") or "0"))
            side = str(getattr(mo, "side", "") or "")
            fee_bps = Decimal(str(getattr(mo, "fee_rate_bps", "0") or "0"))
        else:
            price = Decimal(str(getattr(payload, "price", "0") or "0"))
            size = Decimal(str(getattr(payload, "size", "0") or "0"))
            side = str(getattr(payload, "side", "") or "")
            fee_bps = Decimal(str(getattr(payload, "fee_rate_bps", "0") or "0"))
        if size <= 0:
            return
        fee = (price * size * fee_bps / Decimal("10000")).quantize(Decimal("0.0001"))
        ts_raw = getattr(payload, "timestamp", 0)
        if hasattr(ts_raw, "timestamp"):  # pydantic 可能解析成 datetime
            ts_ms = int(ts_raw.timestamp() * 1000)
        else:
            ts_ms = int(ts_raw or 0)
        fill = FillEvent(
            order_id=client_oid,
            market_id=0,  # 由执行引擎从订单记录回填
            token_id=str(getattr(payload, "token_id", "") or ""),
            side=side,
            price=price,
            qty=size,
            fee=fee,
            ts_ms=ts_ms,
            src="live",
        )
        self.log.info(
            "live fill",
            order=client_oid,
            role=trader_side or "TAKER",
            side=side,
            price=str(price),
            qty=str(size),
            fee=str(fee),
        )
        if self.fill_handler is not None:
            try:
                result = self.fill_handler(fill)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self.log.exception("live fill handler failed", order=client_oid)

    # ── Gateway 接口 ─────────────────────────────────────────
    async def submit(self, intent: OrderIntent) -> str:
        client = await self._ensure_client()
        from polymarket.models.clob.order_response import RejectedOrder

        resp = None
        if intent.tif in ("FOK", "FAK"):
            resp = await client.place_market_order(
                token_id=intent.token_id,
                side=intent.side,
                shares=intent.qty,
                max_price=intent.price if intent.side == "BUY" else None,
                min_price=intent.price if intent.side == "SELL" else None,
                order_type=intent.tif,
            )
        else:
            # 交易所侧一律 GTC（CLOB 要求 GTD ≥3min；本地 deadline 撤单负责窗口截止）
            resp = await client.place_limit_order(
                token_id=intent.token_id,
                price=intent.price,
                size=intent.qty,
                side=intent.side,
                post_only=intent.post_only,
                expiration=None,
            )
        if isinstance(resp, RejectedOrder) or (hasattr(resp, "ok") and not resp.ok):
            raise ValueError(f"clob rejected: {getattr(resp, 'message', '') or getattr(resp, 'code', '')}")
        clob_oid = str(getattr(resp, "order_id", "") or "")
        if clob_oid:
            self._clob_ids[intent.client_order_id] = clob_oid
            self._rev_clob[clob_oid] = intent.client_order_id
        raw_state = str(getattr(resp, "status", "") or "")
        # 归一化到引擎大写状态枚举：maintain()/OPEN_ORDER_STATES 按大写匹配，
        # SDK 返回小写 "live"/"matched"。曾因小写不匹配导致实盘挂单永不被
        # 到期撤单，GTC 单在结算冲刺期超期成交（首日 4+ 笔超期成交的根因之二）。
        norm = {"live": "LIVE", "matched": "FILLED", "filled": "FILLED", "delayed": "LIVE"}
        state = norm.get(raw_state.lower(), raw_state.upper())
        # FAK/FOK 无成交：订单即时终结（exchange 侧立即取消），返回 EXPIRED →
        # 执行引擎 maintain 会补发 on_order_closed，复位策略 pending 锁（防出场卡死）
        if intent.tif in ("FOK", "FAK"):
            trade_ids = getattr(resp, "trade_ids", None) or []
            if not trade_ids and raw_state.lower() not in ("matched", "filled"):
                return "EXPIRED"
        return state or "LIVE"

    def clob_id_for(self, client_order_id: str) -> str | None:
        """网关侧 CLOB 订单号（执行引擎落库诊断用）。"""
        return self._clob_ids.get(client_order_id)

    async def cancel(self, client_order_id: str) -> bool:
        client = await self._ensure_client()
        clob_oid = self._clob_ids.get(client_order_id)
        if clob_oid is None:
            self.log.warning("cancel without clob id (maybe already gone)", order=client_order_id)
            return False
        try:
            resp = await client.cancel_order(order_id=clob_oid)
            self.log.info("LIVE cancel", order=client_order_id, clob=clob_oid, resp=str(resp)[:120])
            return True
        except Exception as e:  # noqa: BLE001
            self.log.warning("live cancel failed", order=client_order_id, err=str(e)[:120])
            return False

    async def cancel_all_market(self, market_id: int) -> int:
        # 由 ExecutionEngine.cancel_all_market 逐单调用 cancel()；此处兜底不实现
        return 0

    # ── 实盘专用 ─────────────────────────────────────────────
    async def startup_safety(self) -> None:
        """启动清理：撤掉上次进程崩溃可能残留的所有挂单（防跨窗口裸仓）。"""
        try:
            client = await self._ensure_client()
            await client.cancel_all()
            self.log.info("startup safety: cancelled all open orders")
        except Exception:
            self.log.exception("startup cancel_all failed")

    async def get_equity(self) -> Decimal:
        """实盘权益 = USDC collateral 余额（风控所有限额的基数）。

        SDK 返回原始单位（USDC 6 位小数 → 除 1e6）。曾漏除导致权益被当成
        9200 万 USDC（实际 92.43），仓位上限会被顶到 100 USDC/笔。
        """
        client = await self._ensure_client()
        ba = await client.get_balance_allowance(asset_type="COLLATERAL")
        raw = Decimal(str(getattr(ba, "balance", "0") or "0"))
        return raw / Decimal("1000000")

    async def close(self) -> None:
        if self._user_task is not None:
            self._user_task.cancel()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
