"""実SDKの通信設定と、利用範囲の資源解放を検証する。"""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from google.genai import errors
from pydantic import SecretStr

from app.ai_providers.gemini import client as module
from app.ai_providers.gemini.settings import GeminiConnectionSettings


@pytest.mark.parametrize(
    "field", ["connect_timeout", "read_timeout", "write_timeout", "pool_timeout"]
)
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "10"])
def test_settings_reject_invalid_timeouts(field, value):
    with pytest.raises((TypeError, ValueError)):
        GeminiConnectionSettings(**{field: value})


def test_settings_are_immutable_and_secret_free():
    settings = GeminiConnectionSettings()
    assert (
        settings.connect_timeout,
        settings.read_timeout,
        settings.write_timeout,
        settings.pool_timeout,
    ) == (3, 10, 10, 3)
    with pytest.raises(FrozenInstanceError):
        settings.read_timeout = 1
    assert "api_key" not in repr(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [200, 429, 500, 503, "timeout", "connect"])
async def test_real_sdk_uses_timeout_and_never_retries(monkeypatch, outcome):
    requests = []
    clients = []

    def respond(request):
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if outcome == "connect":
            raise httpx.ConnectError("private", request=request)
        if outcome != 200:
            return httpx.Response(
                outcome,
                json={
                    "error": {
                        "code": outcome,
                        "message": "private",
                        "status": "UNAVAILABLE",
                    }
                },
            )
        return httpx.Response(200, json={"embeddings": [{"values": [0.1, 0.2]}]})

    def factory(**kwargs):
        assert kwargs.pop("retries") == 0
        assert kwargs["follow_redirects"] is False
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(module, "make_external_async_client", factory)
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    async with module.open_gemini_client(
        api_key=SecretStr("test-private-key"), settings=GeminiConnectionSettings()
    ) as client:
        if outcome == 200:
            result = await client.models.embed_content(
                model="gemini-embedding-001", contents="text"
            )
            assert result.embeddings[0].values == [0.1, 0.2]
        else:
            with pytest.raises(
                (errors.APIError, httpx.TimeoutException, httpx.ConnectError)
            ):
                await client.models.embed_content(
                    model="gemini-embedding-001", contents="text"
                )
    assert len(requests) == 1
    assert requests[0].url.host == "generativelanguage.googleapis.com"
    assert requests[0].extensions["timeout"] == {
        "connect": 3,
        "read": 10,
        "write": 10,
        "pool": 3,
    }
    assert clients[0].is_closed


@pytest.fixture
def resources(monkeypatch):
    http = Mock(spec=httpx.AsyncClient)
    http.aclose = AsyncMock()
    aio = SimpleNamespace(aclose=AsyncMock())
    sdk = SimpleNamespace(aio=aio, close=Mock())
    factory = Mock(return_value=http)
    constructor = Mock(return_value=sdk)
    monkeypatch.setattr(module, "make_external_async_client", factory)
    monkeypatch.setattr(module.genai, "Client", constructor)
    return http, aio, sdk, factory, constructor


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", " ", "\n\t"])
async def test_empty_key_rejected_before_creation(resources, key):
    with pytest.raises(ValueError, match="Gemini API key must not be empty"):
        async with module.open_gemini_client(
            api_key=SecretStr(key), settings=GeminiConnectionSettings()
        ):
            pytest.fail("must not yield")
    resources[3].assert_not_called()
    resources[4].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("private"), asyncio.CancelledError()]
)
async def test_closes_resources_and_preserves_body_exception(resources, failure):
    http, aio, sdk, _, _ = resources

    async def run():
        async with module.open_gemini_client(
            api_key=SecretStr("private"), settings=GeminiConnectionSettings()
        ) as client:
            assert client is aio
            if failure is not None:
                raise failure
        return "completed"

    if failure is None:
        assert await run() == "completed"
    else:
        with pytest.raises(type(failure)) as caught:
            await run()
        assert caught.value is failure
    http.aclose.assert_awaited_once()
    aio.aclose.assert_awaited_once()
    sdk.close.assert_called_once()


@pytest.mark.asyncio
async def test_sdk_creation_failure_closes_http(resources):
    failure = RuntimeError("private")
    resources[4].side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        async with module.open_gemini_client(
            api_key=SecretStr("private"), settings=GeminiConnectionSettings()
        ):
            pytest.fail("must not yield")
    assert caught.value is failure
    resources[0].aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("log_fails", [False, True])
@pytest.mark.parametrize("body_fails", [False, True])
async def test_cleanup_failures_do_not_change_result(
    resources, monkeypatch, log_fails, body_fails
):
    http, aio, sdk, _, _ = resources
    for closer in (http.aclose, aio.aclose, sdk.close):
        closer.side_effect = RuntimeError("private-key")
    log = Mock(side_effect=RuntimeError("log-private") if log_fails else None)
    monkeypatch.setattr(module.logger, "warning", log)
    failure = ValueError("original")

    async def run():
        async with module.open_gemini_client(
            api_key=SecretStr("private-key"), settings=GeminiConnectionSettings()
        ):
            if body_fails:
                raise failure
        return "completed"

    if body_fails:
        with pytest.raises(ValueError) as caught:
            await run()
        assert caught.value is failure
    else:
        assert await run() == "completed"
    assert log.call_count == 3
    assert "private" not in repr(log.call_args_list)
    assert {call.kwargs["resource"] for call in log.call_args_list} == {
        "http",
        "sdk_sync",
        "sdk_async",
    }


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates_and_other_resources_close(resources):
    http, aio, sdk, _, _ = resources
    aio.aclose.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        async with module.open_gemini_client(
            api_key=SecretStr("private"), settings=GeminiConnectionSettings()
        ):
            pass
    sdk.close.assert_called_once()
    http.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_factory_and_sdk_scope_with_custom_settings(monkeypatch):
    from app.http import external

    sent = []
    clients = []
    original = module.make_external_async_client

    async def respond(self, request):
        sent.append(request)
        return httpx.Response(200, json={"embeddings": [{"values": [0.5]}]})

    def factory(**kwargs):
        client = original(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(external._PinnedDnsTransport, "handle_async_request", respond)
    monkeypatch.setattr(module, "make_external_async_client", factory)
    settings = GeminiConnectionSettings(read_timeout=7)
    sdk_clients = []
    for _ in range(2):
        async with module.open_gemini_client(
            api_key=SecretStr("private"), settings=settings
        ) as sdk:
            sdk_clients.append(sdk)
            for _ in range(2):
                await sdk.models.embed_content(
                    model="gemini-embedding-001", contents="text"
                )
            assert not clients[-1].is_closed
        assert clients[-1].is_closed
    assert sdk_clients[0] is not sdk_clients[1]
    assert clients[0] is not clients[1]
    assert len(sent) == 4
    assert all(
        request.extensions["timeout"]
        == {"connect": 3, "read": 7, "write": 10, "pool": 3}
        for request in sent
    )


@pytest.mark.asyncio
async def test_async_accessor_failure_closes_created_resources(resources):
    http, _, _, _, constructor = resources
    failure = RuntimeError("private")

    class PartialClient:
        close = Mock()

        @property
        def aio(self):
            raise failure

    sdk = PartialClient()
    constructor.return_value = sdk
    with pytest.raises(RuntimeError) as caught:
        async with module.open_gemini_client(
            api_key=SecretStr("private"), settings=GeminiConnectionSettings()
        ):
            pytest.fail("must not yield")
    assert caught.value is failure
    sdk.close.assert_called_once()
    http.aclose.assert_awaited_once()
