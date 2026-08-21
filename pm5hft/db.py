"""数据库引擎与会话。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def normalize_db_url(url: str) -> str:
    """SQLite URL → 绝对路径并确保父目录存在（相对/绝对都处理；frozen exe 用绝对路径）。"""
    if not url.startswith("sqlite"):
        return url
    if url.startswith("sqlite+aiosqlite:///./"):
        path = url.split("///./", 1)[1]
    elif url.startswith("sqlite+aiosqlite:///"):
        path = url.split("sqlite+aiosqlite:///", 1)[1]
    else:
        return url
    abs_path = Path(path).resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{abs_path.as_posix()}"


def init_db(url: str) -> None:
    global _engine, _session_factory
    url = normalize_db_url(url)
    _engine = create_async_engine(url, echo=False, poolclass=NullPool)
    if url.startswith("sqlite"):
        # 写连接加 busy_timeout + WAL：面板只读查询/诊断脚本并发时不立即报 locked
        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def session_factory() -> async_sessionmaker[AsyncSession]:
    assert _session_factory is not None, "init_db() not called"
    return _session_factory


async def create_schema() -> None:
    from .models import Base

    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def db_path_from_url(url: str) -> str | None:
    if url.startswith("sqlite"):
        return url.split("///", 1)[-1]
    return None
