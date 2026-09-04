"""API lifespan が Engine と producer / live adapter を所有する契約。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app.agent.live_updates.transport import AgentLiveTransport
from app.agent.runs.enqueuer import AgentRunEnqueuer
from app.main import lifespan


def _stub_lifespan_integrations(
    monkeypatch: pytest.MonkeyPatch, engine: MagicMock
) -> tuple[MagicMock, tuple[MagicMock, MagicMock, MagicMock]]:
    """lifespanの所有契約だけを観測できるよう外部計装と broker を止める。"""
    live = MagicMock()
    live.aclose = AsyncMock()
    producers = tuple(MagicMock() for _ in range(3))
    for broker in producers:
        broker.startup = AsyncMock()
        broker.shutdown = AsyncMock()
    monkeypatch.setattr("app.main.create_api_engine", lambda *_args: engine)
    monkeypatch.setattr(
        "app.main.create_api_agent_live_client",
        lambda *_args, **_kwargs: live,
    )
    monkeypatch.setattr("app.main._API_PRODUCER_BROKERS", producers)
    monkeypatch.setattr("app.main.setup_logfire", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.main.logfire.instrument_fastapi", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "app.main.logfire.instrument_sqlalchemy", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("app.main.log_pool_initialized", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.main.register_pool_metrics", lambda *_args, **_kwargs: None
    )
    return live, producers


def _assert_producers_owned(
    app: FastAPI,
    producers: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    assert isinstance(app.state.agent_live_transport, AgentLiveTransport)
    assert isinstance(app.state.agent_run_enqueuer, AgentRunEnqueuer)
    assert not hasattr(app.state, "agent_live_redis")
    for broker in producers:
        broker.startup.assert_awaited_once()


async def test_lifespan_holds_engine_and_session_factory_then_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    live, producers = _stub_lifespan_integrations(monkeypatch, engine)

    app = FastAPI()
    async with lifespan(app):
        assert app.state.engine is engine
        assert callable(app.state.session_factory)
        _assert_producers_owned(app, producers)

    engine.dispose.assert_awaited_once()
    live.aclose.assert_awaited_once()
    for broker in producers:
        broker.shutdown.assert_awaited_once()


async def test_lifespan_disposes_engine_when_application_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    live, producers = _stub_lifespan_integrations(monkeypatch, engine)

    app = FastAPI()
    with pytest.raises(RuntimeError, match="application failed"):
        async with lifespan(app):
            raise RuntimeError("application failed")

    engine.dispose.assert_awaited_once()
    live.aclose.assert_awaited_once()
    for broker in producers:
        broker.shutdown.assert_awaited_once()


async def test_lifespan_closes_remaining_resources_after_broker_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    live, producers = _stub_lifespan_integrations(monkeypatch, engine)
    producers[1].shutdown = AsyncMock(side_effect=RuntimeError("broker down"))

    app = FastAPI()
    with pytest.raises(RuntimeError, match="broker down"):
        async with lifespan(app):
            pass

    for broker in producers:
        broker.shutdown.assert_awaited_once()
    live.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()
