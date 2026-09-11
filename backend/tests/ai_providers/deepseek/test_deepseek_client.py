"""実SDKの通信設定と、利用範囲の資源解放を検証する。"""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from openai import APIError
from pydantic import SecretStr

from app.ai_providers.deepseek import client as module
from app.ai_providers.deepseek.settings import DeepSeekConnectionSettings
from app.ai_providers.errors import AIProviderConfigurationError


@pytest.mark.parametrize(
    "field", ["connect_timeout", "read_timeout", "write_timeout", "pool_timeout"]
)
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "10"])
def test_settings_reject_invalid_timeouts(field, value):
    """各待機時間に無効な値を指定した場合、クライアント生成前に設定を拒否する。"""
    with pytest.raises((TypeError, ValueError)):
        DeepSeekConnectionSettings(**{field: value})


def test_settings_are_immutable_and_secret_free():
    """既定の待機時間が3・10・10・3秒で固定され、設定が変更不可かつ秘密情報を持たないことを確認する。"""
    settings = DeepSeekConnectionSettings()
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
    """実SDKの送信に4種類の待機上限が適用され、通信・HTTPエラーでも再送しないことを確認する。"""
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
        return httpx.Response(200, json={"choices": []})

    def factory(**kwargs):
        assert kwargs.pop("retries") == 0
        assert kwargs["follow_redirects"] is False
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(module, "make_external_async_client", factory)
    async with module.open_deepseek_client(
        api_key=SecretStr("test-private-key"),
        base_url="https://api.deepseek.com/beta",
        settings=DeepSeekConnectionSettings(),
    ) as client:
        if outcome == 200:
            result = await client.chat.completions.create(model="test", messages=[])
            assert result.choices == []
        else:
            with pytest.raises((APIError, httpx.TimeoutException, httpx.ConnectError)):
                await client.chat.completions.create(model="test", messages=[])
    assert len(requests) == 1
    assert requests[0].url.host == "api.deepseek.com"
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
    http.is_closed = False
    sdk = SimpleNamespace(close=AsyncMock())
    factory = Mock(return_value=http)
    constructor = Mock(return_value=sdk)
    monkeypatch.setattr(module, "make_external_async_client", factory)
    monkeypatch.setattr(module, "AsyncOpenAI", constructor)
    return http, sdk, factory, constructor


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", " ", "\n\t"])
async def test_empty_key_rejected_before_creation(resources, key):
    """空または空白だけのAPIキーでは、HTTP・SDKの資源を生成せず設定エラーにする。"""
    with pytest.raises(AIProviderConfigurationError):
        async with module.open_deepseek_client(
            api_key=SecretStr(key),
            base_url="https://api.deepseek.com/beta",
            settings=DeepSeekConnectionSettings(),
        ):
            pytest.fail("must not yield")
    resources[2].assert_not_called()
    resources[3].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("private"), asyncio.CancelledError()]
)
async def test_closes_resources_and_preserves_body_exception(resources, failure):
    """利用の成功・失敗・キャンセルのいずれでも資源を閉じ、元の例外を保持する。"""
    http, sdk, _, _ = resources

    async def run():
        async with module.open_deepseek_client(
            api_key=SecretStr("private"),
            base_url="https://api.deepseek.com/beta",
            settings=DeepSeekConnectionSettings(),
        ) as client:
            assert client is sdk
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
    sdk.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_sdk_creation_failure_closes_http(resources):
    """SDK生成に失敗した場合も、先に作成したHTTPクライアントを解放する。"""
    failure = RuntimeError("private")
    resources[3].side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        async with module.open_deepseek_client(
            api_key=SecretStr("private"),
            base_url="https://api.deepseek.com/beta",
            settings=DeepSeekConnectionSettings(),
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
    """終了処理と診断ログが失敗しても、残りの終了処理を試みて元の成功・失敗を保持する。"""
    http, sdk, _, _ = resources
    for closer in (http.aclose, sdk.close):
        closer.side_effect = RuntimeError("private-key")
    log = Mock(side_effect=RuntimeError("log-private") if log_fails else None)
    monkeypatch.setattr(module.logger, "warning", log)
    failure = ValueError("original")

    async def run():
        async with module.open_deepseek_client(
            api_key=SecretStr("private-key"),
            base_url="https://api.deepseek.com/beta",
            settings=DeepSeekConnectionSettings(),
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
    assert log.call_count == 2
    assert "private" not in repr(log.call_args_list)
    assert {call.kwargs["resource"] for call in log.call_args_list} == {
        "http",
        "sdk",
    }


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates_and_other_resources_close(resources):
    """SDK終了中のキャンセルを抑止せず、残ったHTTP資源の解放も試みる。"""
    http, sdk, _, _ = resources
    sdk.close.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        async with module.open_deepseek_client(
            api_key=SecretStr("private"),
            base_url="https://api.deepseek.com/beta",
            settings=DeepSeekConnectionSettings(),
        ):
            pass
    sdk.close.assert_awaited_once()
    http.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_factory_and_sdk_scope_with_custom_settings(monkeypatch):
    """実ファクトリで指定値を送信へ反映し、利用範囲ごとに別資源を作ってHTTPを一度だけ閉じる。"""
    from app.http import external

    sent = []
    clients = []
    original = module.make_external_async_client

    async def respond(self, request):
        sent.append(request)
        return httpx.Response(200, json={"choices": []})

    def factory(**kwargs):
        client = original(**kwargs)
        client.aclose = AsyncMock(wraps=client.aclose)
        clients.append(client)
        return client

    monkeypatch.setattr(external._PinnedDnsTransport, "handle_async_request", respond)
    monkeypatch.setattr(module, "make_external_async_client", factory)
    settings = DeepSeekConnectionSettings(read_timeout=7)
    sdk_clients = []
    for _ in range(2):
        async with module.open_deepseek_client(
            api_key=SecretStr("private"),
            base_url="https://api.deepseek.com/beta",
            settings=settings,
        ) as sdk:
            sdk_clients.append(sdk)
            for _ in range(2):
                await sdk.chat.completions.create(model="test", messages=[])
            assert not clients[-1].is_closed
        assert clients[-1].is_closed
        clients[-1].aclose.assert_awaited_once()
    assert sdk_clients[0] is not sdk_clients[1]
    assert clients[0] is not clients[1]
    assert len(sent) == 4
    assert all(
        request.extensions["timeout"]
        == {"connect": 3, "read": 7, "write": 10, "pool": 3}
        for request in sent
    )
