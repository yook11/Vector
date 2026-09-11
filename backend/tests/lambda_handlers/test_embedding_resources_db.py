"""実DBでプール再利用・トランザクション分離・再接続を検証する。"""

import asyncio
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.db import engine as engine_module
from app.db.errors import DatabaseError, DatabaseTimeoutError
from app.lambda_handlers.embedding import resources as module
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from tests.iam_fixtures import inject_test_db_signer


@pytest.fixture
def resource_settings(test_database_url, monkeypatch):
    monkeypatch.setattr(
        module, "get_secret_parameter", Mock(return_value=SecretStr("private"))
    )
    return EmbeddingConsumerSettings(
        env="test",
        database_url=inject_test_db_signer(
            monkeypatch, test_database_url, resources_module=module
        ),
        db_iam_auth=True,
        aws_region="ap-northeast-1",
        gemini_api_key_parameter_path="/key",
    )


@pytest.mark.asyncio
async def test_reuses_connection_rolls_back_and_closes(resource_settings, db_session):
    async with module.open_embedding_resources(resource_settings) as resources:
        async with resources.session_factory() as session:
            pid = await session.scalar(text("select pg_backend_pid()"))
            assert (
                await session.scalar(text("show application_name"))
                == "vector-embedding-consumer"
            )
            await session.execute(
                text("select set_config('vector.scope_test', 'transaction', true)")
            )
        async with resources.session_factory() as session:
            assert await session.scalar(text("select pg_backend_pid()")) == pid
            assert (
                await session.scalar(
                    text("select current_setting('vector.scope_test', true)")
                )
                != "transaction"
            )
        with pytest.raises(DatabaseError):
            async with resources.session_factory() as session:
                await session.execute(text("select 1 / 0"))
        async with resources.session_factory() as session:
            assert await session.scalar(text("select 1")) == 1
    assert (
        await db_session.scalar(
            text("select count(*) from pg_stat_activity where pid=:pid"), {"pid": pid}
        )
        == 0
    )
    async with module.open_embedding_resources(resource_settings) as next_resources:
        assert next_resources.session_factory is not resources.session_factory
        async with next_resources.session_factory() as session:
            assert await session.scalar(text("select pg_backend_pid()")) != pid


@pytest.mark.asyncio
async def test_pool_saturation_is_bounded(resource_settings, monkeypatch):
    create = module.create_embedding_consumer_engine

    def short_wait(*args, **kwargs):
        engine = create(*args, **kwargs)
        engine.pool._timeout = 0.02
        return engine

    monkeypatch.setattr(module, "create_embedding_consumer_engine", short_wait)
    async with module.open_embedding_resources(resource_settings) as resources:
        async with resources.session_factory() as first:
            await first.execute(text("select 1"))
            with pytest.raises(DatabaseTimeoutError):
                async with resources.session_factory() as second:
                    await second.execute(text("select 1"))
        async with resources.session_factory() as session:
            assert await session.scalar(text("select 1")) == 1


@pytest.mark.asyncio
async def test_command_timeout_allows_following_session(resource_settings, monkeypatch):
    create = engine_module._create_engine

    def short_command(*args, **kwargs):
        kwargs["connect_args"]["command_timeout"] = 0.02
        return create(*args, **kwargs)

    monkeypatch.setattr(engine_module, "_create_engine", short_command)
    async with module.open_embedding_resources(resource_settings) as resources:
        with pytest.raises(TimeoutError):
            async with resources.session_factory() as session:
                await session.execute(text("select pg_sleep(1)"))
        async with resources.session_factory() as session:
            assert await session.scalar(text("select 1")) == 1


@pytest.mark.asyncio
async def test_reconnects_after_disconnect(resource_settings, db_session):
    disconnected = asyncio.Event()
    async with module.open_embedding_resources(resource_settings) as resources:
        async with resources.session_factory() as session:
            pid = await session.scalar(text("select pg_backend_pid()"))
            connection = await session.connection()
            raw = await connection.get_raw_connection()
            raw.driver_connection.add_termination_listener(lambda _: disconnected.set())
        assert await db_session.scalar(
            text("select pg_terminate_backend(:pid)"), {"pid": pid}
        )
        await asyncio.wait_for(disconnected.wait(), timeout=2)
        async with resources.session_factory() as session:
            assert await session.scalar(text("select pg_backend_pid()")) != pid
