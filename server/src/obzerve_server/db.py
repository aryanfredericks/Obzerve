from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from obzerve_server.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    connect_args = {
        "timeout": settings.database_pool_timeout_s,
        "server_settings": {"statement_timeout": str(settings.database_statement_timeout_ms)},
    }
    if settings.database_null_pool:
        return create_async_engine(
            settings.database_url, poolclass=NullPool, connect_args=connect_args
        )
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        pool_timeout=settings.database_pool_timeout_s,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session
