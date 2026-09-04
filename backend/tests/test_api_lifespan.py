"""API lifespan が Engine と Session factory を所有する契約。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app.main import lifespan


def _stub_lifespan_integrations(
    monkeypatch: pytest.MonkeyPatch, engine: MagicMock
) -> MagicMock:
    """lifespanのDB所有契約だけを観測できるよう外部計装を止める。"""
    live = MagicMock()
    live.aclose = AsyncMock()
    monkeypatch.setattr("app.main.create_api_engine", lambda *_args: engine)
    monkeypatch.setattr(
        "app.main.create_api_agent_live_client",
        lambda *_args, **_kwargs: live,
    )
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
    return live


async def test_lifespan_holds_engine_and_session_factory_then_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    live = _stub_lifespan_integrations(monkeypatch, engine)

    app = FastAPI()
    async with lifespan(app):
        assert app.state.engine is engine
        assert callable(app.state.session_factory)
        assert app.state.agent_live_redis is live

    engine.dispose.assert_awaited_once()
    live.aclose.assert_awaited_once()


async def test_lifespan_disposes_engine_when_application_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    live = _stub_lifespan_integrations(monkeypatch, engine)

    app = FastAPI()
    with pytest.raises(RuntimeError, match="application failed"):
        async with lifespan(app):
            raise RuntimeError("application failed")

    engine.dispose.assert_awaited_once()
    live.aclose.assert_awaited_once()
