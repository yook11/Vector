"""brokers.py の composition root と worker runtime 設定に関するテスト。"""

import configparser
import re
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from taskiq import TaskiqEvents, TaskiqState

import app.db.engine as db_engine
from app.config import settings
from app.db.engine import (
    DEFAULT_POOL_RECYCLE,
    WORKER_POOL_SIZING,
    create_worker_engine,
    worker_service_name,
)

# supervisord の worker 定義 (taskiq worker 起動引数の SSoT)。
_SUPERVISORD_DIR = Path(__file__).resolve().parent.parent / "supervisord"


def _parse_worker_programs() -> dict[str, int | None]:
    """supervisord ``*.conf`` の worker を ``{label: max_async_tasks}`` に解す。

    ``label`` は既存 ``broker_<label>`` またはprocess factory module名から取り、
    ``--max-async-tasks`` 不在は ``None`` (明示漏れ) を返す。
    ``taskiq scheduler`` / eventlistener はworkerでないため対象外。
    """
    workers: dict[str, int | None] = {}
    for conf in sorted(_SUPERVISORD_DIR.glob("*.conf")):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(conf)
        for section in parser.sections():
            if not section.startswith("program:"):
                continue
            command = parser[section].get("command", "")
            if "taskiq worker" not in command:
                continue
            label_match = re.search(r"broker_(\w+)", command) or re.search(
                r"app\.insights\.(trend_discovery)\.worker:create_broker",
                command,
            )
            assert label_match, f"{section}: broker module not found in command"
            max_match = re.search(r"--max-async-tasks\s+(\d+)", command)
            workers[label_match.group(1)] = (
                int(max_match.group(1)) if max_match else None
            )
    return workers


@pytest.mark.asyncio
async def test_wire_briefing_adapter_attaches_generator_to_state() -> None:
    """broker_briefing 起動時に briefing generator が state へ attach される。

    briefing の AI provider 選択も composition root で hardcode する設計 (Pure DI) を
    構造的に保証する (analysis と同じ集約点)。
    """
    from app.insights.briefing.llm import DeepSeekBriefingGenerator
    from app.queue.composition import _wire_briefing_adapter

    state = TaskiqState()
    with patch("app.insights.briefing.llm.settings") as mock_settings:
        mock_settings.deepseek_api_key = SecretStr("test-key")
        await _wire_briefing_adapter(state)

    assert isinstance(state.briefing_generator, DeepSeekBriefingGenerator)


class TestWorkerMaxAsyncTasksCeiling:
    """全 worker が ``--max-async-tasks`` を明示し、各値が pool cap 以下に収まる。

    通常パスの上限ガード。狙いは taskiq 既定 (100) への暗黙依存を断ち、起動時 backlog の
    thundering herd で pool が即枯渇するのを防ぐこと。error-path で別 audit session を
    開く経路があり、これは飽和不可能の証明ではない。1 task が瞬間的に 2 connection を
    握りうる分は ``max_overflow`` + ``pool_timeout`` fail-fast で吸収する前提。
    """

    def test_every_worker_declares_max_async_tasks(self) -> None:
        # taskiq 既定 100 への暗黙依存を禁止: 全 worker が並列度を明示する
        missing = [label for label, m in _parse_worker_programs().items() if m is None]
        assert not missing, f"workers without explicit --max-async-tasks: {missing}"

    def test_max_async_tasks_within_pool_cap(self) -> None:
        # 各 worker の同時実行が pool cap (pool_size + max_overflow) を超えない。
        # 境界: briefing=10<=10, trend_discovery=2<=4。
        # cap を下げると当該 worker が落ちる。
        for label, max_async in _parse_worker_programs().items():
            pool_size, max_overflow = WORKER_POOL_SIZING[label]
            cap = pool_size + max_overflow
            assert max_async is not None and max_async <= cap, (
                f"{label}: --max-async-tasks {max_async} exceeds pool cap {cap}"
            )


class TestWorkerPoolSizing:
    """worker engine が ``WORKER_POOL_SIZING`` どおりに作られ deploy 集合と一致する。"""

    def test_sizing_keys_match_deployed_workers(self) -> None:
        # WORKER_POOL_SIZING が supervisord の deploy worker 集合と一致する
        # (新 worker の sizing 追加漏れ / stale entry を構造的に検出する)
        assert set(_parse_worker_programs()) == set(WORKER_POOL_SIZING)

    def test_common_worker_pool_sizing(self) -> None:
        # 共通 worker は pool_size=5 / max_overflow=5 (cap 10) の均一小型
        pool = create_worker_engine(settings, "briefing").sync_engine.pool
        assert (pool.size(), pool._max_overflow) == (5, 5)

    def test_trend_discovery_pool_sizing(self) -> None:
        # trend_discovery のみ 2/2 に縮小 (日次・fan-out なし・最大 1 connection)
        pool = create_worker_engine(settings, "trend_discovery").sync_engine.pool
        assert (pool.size(), pool._max_overflow) == (2, 2)

    def test_worker_recycle_uses_factory_default(self) -> None:
        pool = create_worker_engine(settings, "briefing").sync_engine.pool
        assert pool._recycle == DEFAULT_POOL_RECYCLE == 3600


class TestWorkerApplicationName:
    """worker engine の application_name を検証する。"""

    def test_application_name_matches_service_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _spy(clean_url: str, **kw: Any) -> Any:
            captured.update(kw)
            return _real_create_async_engine(clean_url, **kw)

        monkeypatch.setattr(db_engine, "create_async_engine", _spy)
        create_worker_engine(settings, "briefing")
        server_settings = captured["connect_args"]["server_settings"]
        assert server_settings["application_name"] == worker_service_name("briefing")


class TestSocketTimeout:
    """redis-py 8 の既定 5 秒 socket_timeout の明示上書きを pin する。

    listener の blocking read (XREADGROUP) が既定 timeout で切れると worker
    プロセスごと落ちるため、read は 30 秒へ広げ、接続確立のみ 5 秒の fail-fast を
    保つ。全 broker は ``_make_broker`` を経由して同じ kwargs を共有するため、
    broker_briefing 1 つを代表として検証する。期待値は production 定数の参照では
    なくリテラルで pin し、値の変更を意図的な差分として顕在化させる。
    """

    def test_broker_connection_pool_has_socket_timeout(self) -> None:
        from app.queue.brokers import broker_briefing

        kwargs = broker_briefing.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == 30
        assert kwargs["socket_connect_timeout"] == 5

    def test_broker_uses_dummy_result_backend(self) -> None:
        from taskiq.result_backends.dummy import DummyResultBackend

        from app.queue.brokers import broker_briefing

        assert isinstance(broker_briefing.result_backend, DummyResultBackend)


def _owned_redis() -> MagicMock:
    redis = MagicMock()
    redis.aclose = AsyncMock()
    return redis


def _session_factory_stub() -> MagicMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    return MagicMock(return_value=session)


@asynccontextmanager
async def _worker_lifecycle_stubs(
    engine: MagicMock,
    *,
    live: MagicMock | None = None,
    compose: bool = False,
):
    patches = [
        patch("app.queue.lifecycle.setup_logfire"),
        patch("app.queue.lifecycle.create_worker_engine", return_value=engine),
        patch("app.queue.lifecycle.logfire.instrument_sqlalchemy"),
        patch("app.queue.lifecycle.log_pool_initialized"),
        patch("app.queue.lifecycle.register_pool_metrics"),
        patch(
            "app.queue.lifecycle.caller_managed_session_factory",
            return_value=_session_factory_stub(),
        ),
    ]
    if not compose:
        patches.append(patch("app.queue.lifecycle._compose", new_callable=AsyncMock))
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        create_live = stack.enter_context(
            patch(
                "app.queue.lifecycle.create_worker_agent_live_client",
                return_value=live if live is not None else MagicMock(),
            )
        )
        yield create_live


@pytest.mark.asyncio
async def test_agent_worker_owns_only_agent_live_redis() -> None:
    from app.queue.brokers import broker_agent

    engine = MagicMock()
    engine.dispose = AsyncMock()
    live = _owned_redis()
    state = TaskiqState()

    async with _worker_lifecycle_stubs(engine, live=live) as create_live:
        await broker_agent.event_handlers[TaskiqEvents.WORKER_STARTUP][0](state)
        await broker_agent.event_handlers[TaskiqEvents.WORKER_SHUTDOWN][0](state)

    create_live.assert_called_once_with(settings)
    assert state.agent_live_redis is live
    live.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_shutdown_closes_remaining_resources_after_redis_failure() -> None:
    from app.queue.brokers import broker_agent

    engine = MagicMock()
    engine.dispose = AsyncMock()
    live = _owned_redis()
    live.aclose = AsyncMock(side_effect=RuntimeError("redis close failed"))
    state = TaskiqState()

    async with _worker_lifecycle_stubs(engine, live=live):
        await broker_agent.event_handlers[TaskiqEvents.WORKER_STARTUP][0](state)
        with pytest.raises(RuntimeError, match="redis close failed"):
            await broker_agent.event_handlers[TaskiqEvents.WORKER_SHUTDOWN][0](state)

    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_broker_shutdown_disconnects_pool_after_hook_error() -> None:
    from app.queue.brokers import _make_broker

    broker = _make_broker("test-shutdown-disconnect")
    broker.is_worker_process = True
    disconnect = AsyncMock()
    broker.connection_pool.disconnect = disconnect

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def _fail(_state: TaskiqState) -> None:
        raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError, match="hook failed"):
        await broker.shutdown()

    disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_worker", "is_scheduler", "should_declare"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
    ],
)
async def test_broker_startup_declares_consumer_group_only_on_worker_or_scheduler(
    is_worker: bool,
    is_scheduler: bool,
    should_declare: bool,
) -> None:
    from app.queue.brokers import _make_broker

    broker = _make_broker("test-xgroup-gate")
    broker.is_worker_process = is_worker
    broker.is_scheduler_process = is_scheduler
    declare = AsyncMock()
    broker._declare_consumer_group = declare
    try:
        await broker.startup()
        if should_declare:
            declare.assert_awaited_once()
        else:
            declare.assert_not_called()
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_agent_worker_owns_deadline_schedule_source(monkeypatch):
    # agentだけが予約sourceを起動・終了する。
    from app.queue.brokers import broker_agent, broker_briefing

    engine = MagicMock(dispose=AsyncMock())
    source = MagicMock(startup=AsyncMock(), shutdown=AsyncMock())
    factory = MagicMock(return_value=source)
    monkeypatch.setattr("app.queue.lifecycle.create_deadline_schedule_source", factory)
    async with _worker_lifecycle_stubs(engine, live=_owned_redis()):
        for broker in (broker_agent, broker_briefing):
            state = TaskiqState()
            await broker.event_handlers[TaskiqEvents.WORKER_STARTUP][0](state)
            assert hasattr(state, "agent_deadline_scheduler") == (
                broker is broker_agent
            )
            await broker.event_handlers[TaskiqEvents.WORKER_SHUTDOWN][0](state)
    factory.assert_called_once_with(settings)
    source.startup.assert_awaited_once()
    source.shutdown.assert_awaited_once()


@pytest.fixture
def gemini_http_clients(monkeypatch):
    """Workerが所有する実Gemini HTTPクライアントの終了を観測する。"""
    from app.ai_providers.gemini import client as module

    clients = []
    create = module.make_external_async_client

    def track(**kwargs):
        client = create(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(module, "make_external_async_client", track)
    return clients
