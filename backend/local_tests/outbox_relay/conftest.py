"""準備用ownerと配送ロールの接続を組み立てる。"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import caller_managed_session_factory


@pytest.fixture
async def owner_engine(system_database):
    engine = create_async_engine(system_database.url("vector", sqlalchemy=True))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def owner_sessions(owner_engine):
    return async_sessionmaker(owner_engine, expire_on_commit=False)


@pytest.fixture
async def relay_engine(system_database):
    engine = create_async_engine(
        system_database.url("vector_outbox_relay", sqlalchemy=True)
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def relay_sessions(relay_engine):
    return caller_managed_session_factory(relay_engine)
