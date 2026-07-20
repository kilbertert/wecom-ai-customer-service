"""Async SQLAlchemy runtime for the Bug 反馈 relational fact store."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base shared by Alembic and application models."""


def _normalize_async_url(url: str) -> str:
    raw = (url or "").strip()
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    if raw.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + raw.removeprefix("sqlite:///")
    return raw


def _engine_kwargs(url: str) -> dict:
    kwargs = {
        "echo": bool(settings.database.echo),
        "pool_pre_ping": True,
    }
    if not url.startswith("sqlite+"):
        kwargs.update(
            pool_size=max(1, int(settings.database.pool_size)),
            max_overflow=max(0, int(settings.database.max_overflow)),
        )
    return kwargs


DATABASE_URL = _normalize_async_url(settings.database.url)
engine: AsyncEngine = create_async_engine(DATABASE_URL, **_engine_kwargs(DATABASE_URL))
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Open a transaction and commit/rollback it as one unit."""

    async with AsyncSessionLocal() as session:
        async with session.begin():
            yield session


async def verify_database() -> None:
    """Fail startup early when the configured database cannot be reached."""

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def close_database() -> None:
    await engine.dispose()

