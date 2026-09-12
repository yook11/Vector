"""記事単位AI分析の共通ライフサイクルを直接呼び、実DB資源の所有と解放を確認する。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import _create_engine
from app.db.errors import DatabaseError, DatabaseTimeoutError
from app.lambda_handlers import article_analysis_lifecycle as module
from tests.iam_fixtures import inject_test_db_signer


@dataclass
class ClientProbe:
    api_key: SecretStr
    closed: bool = False


@dataclass
class BorrowedConsumer:
    session_factory: module.SessionFactory
    client: ClientProbe


@dataclass
class InvocationResources:
    order: list[str] = field(default_factory=list)
    connection_ids: set[int] = field(default_factory=set)
    checked_out: set[int] = field(default_factory=set)
    engine: AsyncEngine | None = None
    client: ClientProbe | None = None
    checked_out_before_dispose: int | None = None
    disposed: bool = False
    rds_closed: bool = False


@pytest.fixture
async def lifecycle(system_database, monkeypatch):
    url = inject_test_db_signer(
        monkeypatch,
        system_database.url("vector_app", sqlalchemy=True),
        resources_module=module,
    )
    sdk_session = module.Session()
    scopes = []
    recorder = Mock(spec=module.ArticleAnalysisLifecycleRecorder)
    state = SimpleNamespace(
        scopes=scopes,
        preparation_order=[],
        recorder=recorder,
        client_enter_error=None,
        consumer_error=None,
        connect_before_client=False,
    )
    secret = Mock(return_value=SecretStr("test-key"))

    def load_secret(**kwargs):
        state.preparation_order.append("secret")
        return secret(**kwargs)

    monkeypatch.setattr(module, "get_secret_parameter", load_secret)
    state.secret = secret

    def create_rds(*args, **kwargs):
        state.preparation_order.append("rds")
        scope = InvocationResources(order=["rds"])
        scopes.append(scope)
        rds = sdk_session.create_client(*args, **kwargs)
        close = rds.close

        def close_rds():
            close()
            scope.rds_closed = True
            scope.order.append("rds_close")

        rds.close = close_rds
        return rds

    monkeypatch.setattr(
        module, "Session", lambda: SimpleNamespace(create_client=create_rds)
    )

    def create_engine(*, password_provider: module.IamPasswordProvider) -> AsyncEngine:
        scope = scopes[-1]
        engine = _create_engine(
            url,
            password_provider=password_provider,
            application_name="test-article-analysis-lifecycle",
            pool_size=1,
            max_overflow=0,
            pool_timeout=5,
            connect_args={"timeout": 5, "command_timeout": 5},
        )
        scope.engine = engine
        scope.order.append("engine")
        state.preparation_order.append("engine")

        @event.listens_for(engine.sync_engine, "connect")
        def connected(connection, _):
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT pg_backend_pid()")
                scope.connection_ids.add(cursor.fetchone()[0])
            finally:
                cursor.close()

        @event.listens_for(engine.sync_engine, "checkout")
        def checked_out(connection, record, proxy):
            scope.checked_out.add(id(record))

        @event.listens_for(engine.sync_engine, "checkin")
        def checked_in(connection, record):
            scope.checked_out.discard(id(record))

        return engine

    session_factory = module.caller_managed_session_factory

    def create_session_factory(engine):
        state.preparation_order.append("session_factory")
        return session_factory(engine)

    monkeypatch.setattr(
        module, "caller_managed_session_factory", create_session_factory
    )
    dispose = AsyncEngine.dispose

    async def dispose_engine(engine, *args, **kwargs):
        scope = next(scope for scope in scopes if scope.engine is engine)
        scope.checked_out_before_dispose = engine.pool.checkedout()
        await dispose(engine, *args, **kwargs)
        scope.disposed = True
        scope.order.append("engine_close")

    monkeypatch.setattr(AsyncEngine, "dispose", dispose_engine)

    @asynccontextmanager
    async def open_client(*, api_key: SecretStr) -> AsyncIterator[ClientProbe]:
        scope = scopes[-1]
        if state.connect_before_client:
            async with scope.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        if state.client_enter_error is not None:
            raise state.client_enter_error
        scope.client = ClientProbe(api_key)
        scope.order.append("client")
        state.preparation_order.append("client")
        try:
            yield scope.client
        finally:
            scope.client.closed = True
            scope.order.append("client_close")

    def build_consumer(
        *, session_factory: module.SessionFactory, client: ClientProbe
    ) -> BorrowedConsumer:
        state.preparation_order.append("consumer")
        if state.consumer_error is not None:
            raise state.consumer_error
        return BorrowedConsumer(session_factory, client)

    def open_scope():
        return module.open_article_analysis_consumer(
            aws_region="ap-northeast-1",
            database_url=url,
            api_key_parameter_path="/test/article-analysis-key",
            create_engine=create_engine,
            open_client=open_client,
            build_consumer=build_consumer,
            failure_recorder=recorder,
        )

    state.open = open_scope
    try:
        yield state
    finally:
        for scope in scopes:
            if scope.engine is not None:
                await dispose(scope.engine)


async def assert_released(database, scope, *, expected_order=None):
    """終了順と貸出返却を確認し、別接続から実DB接続の消滅を確認する。"""
    closed = [name for name in scope.order if name.endswith("_close")]
    assert closed == (expected_order or ["client_close", "engine_close", "rds_close"])
    assert not scope.checked_out
    assert scope.checked_out_before_dispose == 0
    assert scope.disposed and scope.rds_closed
    if scope.client is not None:
        assert scope.client.closed
    async with database.connect("vector_app") as connection:
        deadline = asyncio.get_running_loop().time() + 2
        while await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid=ANY($1::integer[])",
            list(scope.connection_ids),
        ):
            assert asyncio.get_running_loop().time() < deadline, (
                "利用終了後もDB接続が残っている"
            )
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_resources_stay_open_until_borrower_exits(lifecycle, system_database):
    """借用中の資源を保持し、利用終了時にAI・Engine・RDSの順で解放する。"""
    context = lifecycle.open()
    assert not lifecycle.scopes
    lifecycle.secret.assert_not_called()
    async with context as consumer:
        scope = lifecycle.scopes[-1]
        assert lifecycle.preparation_order == [
            "secret",
            "rds",
            "engine",
            "session_factory",
            "client",
            "consumer",
        ]
        assert scope.order == ["rds", "engine", "client"]
        assert consumer.client is scope.client
        async with consumer.session_factory() as session:
            await session.execute(text("SELECT 1"))
            assert len(scope.checked_out) == 1
        assert scope.connection_ids
        assert not scope.checked_out
        assert (
            not scope.disposed and not scope.rds_closed and not consumer.client.closed
        )
    await assert_released(system_database, scope)


@pytest.mark.asyncio
async def test_invocations_share_within_scope_and_recreate_between_scopes(
    lifecycle, system_database
):
    """呼び出し内で1接続を再利用し、次の呼び出しには資源を持ち越さない。"""
    consumers = []
    lifecycle.secret.side_effect = [SecretStr("first"), SecretStr("second")]
    for _ in range(2):
        async with lifecycle.open() as consumer:
            consumers.append(consumer)
            pids = []
            for _ in range(2):
                async with consumer.session_factory() as session:
                    pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
            assert pids[0] == pids[1]
        await assert_released(system_database, lifecycle.scopes[-1])
    first, second = lifecycle.scopes
    assert first.engine is not second.engine
    assert first.client is not second.client
    assert consumers[0].session_factory is not consumers[1].session_factory
    assert first.connection_ids.isdisjoint(second.connection_ids)
    assert lifecycle.secret.call_count == 2
    assert [consumer.client.api_key for consumer in consumers] == [
        SecretStr("first"),
        SecretStr("second"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ValueError("borrower-failed"), asyncio.CancelledError()]
)
async def test_borrower_failure_releases_resources_and_preserves_exception(
    lifecycle, system_database, failure
):
    """利用中の例外やキャンセルでも実接続を閉じ、同じ例外を伝播する。"""
    with pytest.raises(type(failure)) as caught:
        async with lifecycle.open() as consumer:
            async with consumer.session_factory() as session:
                await session.execute(text("SELECT 1"))
                raise failure
    assert caught.value is failure
    lifecycle.recorder.record_initialization_failure.assert_not_called()
    await assert_released(system_database, lifecycle.scopes[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["client_enter", "consumer"])
async def test_initialization_failure_releases_acquired_database_connections(
    lifecycle, system_database, phase
):
    """実DB接続を取得した後の準備失敗でも、取得済み資源を解放する。"""
    failure = RuntimeError("initialization-failed")
    lifecycle.connect_before_client = True
    setattr(lifecycle, f"{phase}_error", failure)
    with pytest.raises(RuntimeError) as caught:
        async with lifecycle.open():
            pytest.fail("準備失敗時に貸し出してはいけない")
    assert caught.value is failure
    lifecycle.recorder.record_initialization_failure.assert_called_once_with(
        "ai_client" if phase == "client_enter" else "consumer", failure
    )
    scope = lifecycle.scopes[-1]
    assert scope.connection_ids
    expected = (
        ["engine_close", "rds_close"]
        if phase == "client_enter"
        else ["client_close", "engine_close", "rds_close"]
    )
    await assert_released(system_database, scope, expected_order=expected)


@pytest.mark.asyncio
async def test_database_error_releases_connections_and_allows_next_invocation(
    lifecycle, system_database
):
    """実SQLエラーでも接続を閉じ、次の呼び出しで新しい接続を利用できる。"""
    with pytest.raises(DatabaseError):
        async with lifecycle.open() as consumer:
            async with consumer.session_factory() as session:
                await session.execute(text("SELECT 1 / 0"))
    await assert_released(system_database, lifecycle.scopes[-1])
    async with lifecycle.open() as consumer:
        async with consumer.session_factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    await assert_released(system_database, lifecycle.scopes[-1])


@pytest.mark.asyncio
async def test_rollback_isolated_between_borrowed_sessions(lifecycle):
    """同じ接続を再利用しても前セッションの未確定トランザクションを引き継がない。"""
    async with lifecycle.open() as consumer:
        async with consumer.session_factory() as session:
            await session.execute(
                text("SELECT set_config('vector.scope_test', 'transaction', true)")
            )
        async with consumer.session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT current_setting('vector.scope_test', true)")
                )
                != "transaction"
            )


@pytest.mark.asyncio
async def test_pool_saturation_is_bounded(lifecycle, system_database):
    """プール飽和で待機が失敗しても後続セッションを利用でき、終了時に回収する。"""
    async with lifecycle.open() as consumer:
        scope = lifecycle.scopes[-1]
        scope.engine.pool._timeout = 0.02
        async with consumer.session_factory() as first:
            await first.execute(text("SELECT 1"))
            with pytest.raises(DatabaseTimeoutError):
                async with consumer.session_factory() as second:
                    await second.execute(text("SELECT 1"))
        async with consumer.session_factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    await assert_released(system_database, scope)


@pytest.mark.asyncio
async def test_command_timeout_releases_connections(
    lifecycle, system_database, monkeypatch
):
    """実SQLのタイムアウト後にもセッションを利用でき、呼び出し終了時に回収する。"""
    create = _create_engine

    def short_command(*args, **kwargs):
        kwargs["connect_args"]["command_timeout"] = 0.02
        return create(*args, **kwargs)

    monkeypatch.setattr(__name__ + "._create_engine", short_command)
    async with lifecycle.open() as consumer:
        with pytest.raises(TimeoutError):
            async with consumer.session_factory() as session:
                await session.execute(text("SELECT pg_sleep(1)"))
        async with consumer.session_factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    await assert_released(system_database, lifecycle.scopes[-1])


@pytest.mark.asyncio
async def test_reconnects_after_disconnect(lifecycle, system_database):
    """切断された接続を再取得し、再接続した実DB接続も利用終了時に回収する。"""
    disconnected = asyncio.Event()
    async with lifecycle.open() as consumer:
        async with consumer.session_factory() as session:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            connection = await session.connection()
            raw = await connection.get_raw_connection()
            raw.driver_connection.add_termination_listener(lambda _: disconnected.set())
        async with system_database.connect("vector_app") as monitor:
            assert await monitor.fetchval("SELECT pg_terminate_backend($1)", pid)
        await asyncio.wait_for(disconnected.wait(), 2)
        async with consumer.session_factory() as session:
            assert await session.scalar(text("SELECT pg_backend_pid()")) != pid
    await assert_released(system_database, lifecycle.scopes[-1])
