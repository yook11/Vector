"""実SDKへの接続でCuratorの借用契約と共有通信設定を確認する。"""

import json

import httpx
import pytest
from pydantic import SecretStr

from app.ai_providers.errors import (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
)
from app.ai_providers.gemini import client as module
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.curation.ai.gemini import GeminiCurator
from app.analysis.curation.domain import Noise, Signal

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["signal", "noise", "timeout", "unavailable"])
async def test_curator_uses_borrowed_sdk_with_shared_timeout_and_no_retry(
    monkeypatch, outcome
):
    requests = []
    clients = []

    def respond(request):
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("private-timeout", request=request)
        if outcome == "unavailable":
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": 503,
                        "message": "private-unavailable",
                        "status": "UNAVAILABLE",
                    }
                },
            )
        text = json.dumps(
            {"relevance": outcome, "title_ja": "タイトル", "summary_ja": "要約"}
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    def factory(**kwargs):
        assert kwargs.pop("retries") == 0
        assert kwargs["follow_redirects"] is False
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(module, "make_external_async_client", factory)
    async with module.open_gemini_client(
        api_key=SecretStr("test-key"), settings=GeminiConnectionSettings()
    ) as client:
        curator = GeminiCurator(client=client)
        if outcome in ("timeout", "unavailable"):
            error_type = (
                AIProviderNetworkError
                if outcome == "timeout"
                else AIProviderServiceUnavailableError
            )
            with pytest.raises(error_type):
                await curator.curate(title="Title", content="Body")
        else:
            call = await curator.curate(title="Title", content="Body")
            assert isinstance(call.result, Signal if outcome == "signal" else Noise)
            assert call.model_name == curator.model_name
            assert call.prompt_version == curator.prompt_version
        assert not clients[0].is_closed
    assert clients[0].is_closed
    assert len(requests) == 1
    assert requests[0].extensions["timeout"] == {
        "connect": 3,
        "read": 10,
        "write": 10,
        "pool": 3,
    }
    assert requests[0].url.host == "generativelanguage.googleapis.com"
    request_body = json.loads(requests[0].content)
    assert request_body["generationConfig"]["responseSchema"]["required"] == [
        "relevance",
        "title_ja",
        "summary_ja",
    ]
