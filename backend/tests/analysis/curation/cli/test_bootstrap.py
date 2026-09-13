"""CLIの既存処理を維持し、借用Curatorの資源所有だけを検証する。"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.curation.cli import re_curate_all as module

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("failure_stage", [None, "client", "run"])
def test_bootstrap_owns_client_until_run_finishes(monkeypatch, failure_stage):
    order = []
    client = Mock()
    original = RuntimeError("bootstrap-failed")

    async def dispose():
        order.append("engine_close")

    engine = Mock(dispose=AsyncMock(side_effect=dispose))
    monkeypatch.setattr(module, "create_cli_engine", Mock(return_value=engine))
    factory = Mock()
    monkeypatch.setattr(
        module, "caller_managed_session_factory", Mock(return_value=factory)
    )

    @asynccontextmanager
    async def open_client(**kwargs):
        assert kwargs == {
            "api_key": module.settings.gemini_api_key,
            "settings": GeminiConnectionSettings(),
        }
        order.append("client_open")
        if failure_stage == "client":
            raise original
        try:
            yield client
        finally:
            order.append("client_close")

    monkeypatch.setattr(module, "open_gemini_client", open_client)

    async def run(args, service, curator, session_factory):
        order.append("run")
        assert not args.execute
        assert args.max_retries == 4
        assert curator._client is client
        assert session_factory is factory
        if failure_stage == "run":
            raise original
        return 3

    run_mock = AsyncMock(side_effect=run)
    monkeypatch.setattr(module, "run", run_mock)
    if failure_stage:
        with pytest.raises(RuntimeError) as caught:
            module.main(["--limit", "3", "--max-retries", "4"])
        assert caught.value is original
    else:
        assert module.main(["--limit", "3", "--max-retries", "4"]) == 3
    if failure_stage == "client":
        assert order == ["client_open", "engine_close"]
        run_mock.assert_not_awaited()
    else:
        assert order == ["client_open", "run", "client_close", "engine_close"]
        client.close.assert_not_called()
        client.aclose.assert_not_called()
