"""共通HTTP経路を使う実Gemini SDKの要求・応答・終了契約を検証する。"""

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from google.genai import errors, types
from httpcore._backends.auto import AutoBackend
from pydantic import SecretStr

from app.agent import composition
from app.agent.runtime.gemini import GeminiAgentRuntime
from app.ai_providers.errors import AIProviderResponseError, AIProviderResponseReason
from app.ai_providers.gemini import client as module
from app.ai_providers.gemini.error_translator import translate_gemini_error
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.embedder import GeminiEmbedder
from tests.agent.runtime._helpers import RuntimeOutput, make_agent

_MODEL = "gemini-test-model"
_TEXT = "synthetic user request"
_GENERATED = {
    "candidates": [
        {"content": {"parts": [{"text": "generated text"}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
}


@dataclass
class ResponseBody:
    chunks: tuple[bytes, ...] = ()
    closed: bool = False
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event | None = None

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.resume is not None:
            self.waiting.set()
            await self.resume.wait()

    async def aclose(self):
        self.closed = True


@dataclass
class Exchange:
    status: int = 200
    headers: list[tuple[bytes, bytes]] = field(
        default_factory=lambda: [(b"content-type", b"application/json")]
    )
    body: ResponseBody = field(default_factory=ResponseBody)
    requests: list[httpcore.Request] = field(default_factory=list)
    request_bodies: list[dict] = field(default_factory=list)
    clients: list[httpx.AsyncClient] = field(default_factory=list)
    error: Exception | None = None

    def respond_json(self, payload):
        self.body.chunks = (json.dumps(payload).encode(),)

    def respond_sse(self, *payloads):
        self.headers = [(b"content-type", b"text/event-stream")]
        self.body.chunks = tuple(
            f"data: {json.dumps(payload)}\n\n".encode() for payload in payloads
        )


@pytest.fixture
def exchange(monkeypatch):
    """DNSとproxy送受信を模擬し、注入client以外からの送信を拒否する。"""
    result = Exchange()
    result.respond_json(_GENERATED)
    monkeypatch.setenv("EGRESS_PROXY_URL", "http://proxy.vector.internal:3128")
    monkeypatch.setattr(
        "app.http.destination_resolution._resolve_host",
        AsyncMock(return_value=["93.184.216.34"]),
    )
    original_factory = module.make_external_async_client
    original_send = httpx.AsyncClient.send

    def factory(**kwargs):
        client = original_factory(**kwargs)
        result.clients.append(client)
        return client

    async def send_only_injected(self, *args, **kwargs):
        assert self in result.clients, "SDK bypassed the injected HTTP client"
        return await original_send(self, *args, **kwargs)

    def reject_sync_send(*args, **kwargs):
        pytest.fail("SDK attempted synchronous HTTP I/O")

    async def respond(self, request):
        result.requests.append(request)
        raw = b"".join([part async for part in request.stream])
        result.request_bodies.append(json.loads(raw))
        if result.error is not None:
            raise result.error
        return httpcore.Response(
            result.status, headers=result.headers, content=result.body
        )

    monkeypatch.setattr(module, "make_external_async_client", factory)
    monkeypatch.setattr(httpx.AsyncClient, "send", send_only_injected)
    monkeypatch.setattr(httpx.Client, "send", reject_sync_send)
    monkeypatch.setattr(httpcore.AsyncHTTPProxy, "handle_async_request", respond)
    return result


def _open_client():
    return module.open_gemini_client(
        api_key=SecretStr("synthetic-test-key"),
        settings=GeminiConnectionSettings(
            connect_timeout=2, read_timeout=7, write_timeout=11, pool_timeout=3
        ),
    )


async def _generate(client):
    return await client.models.generate_content(model=_MODEL, contents=_TEXT)


@pytest.mark.asyncio
async def test_generation_sends_declared_model_prompt_and_options(exchange):
    """生成要求のmodel・入力・指示・オプションをwire上で維持する。"""
    async with _open_client() as client:
        await client.models.generate_content(
            model=_MODEL,
            contents=_TEXT,
            config=types.GenerateContentConfig(
                system_instruction="synthetic system instruction",
                temperature=0.25,
                max_output_tokens=321,
            ),
        )
    assert len(exchange.requests) == 1
    request = exchange.requests[0]
    assert request.method == b"POST"
    assert request.url.host == b"generativelanguage.googleapis.com"
    assert request.url.target == b"/v1beta/models/gemini-test-model:generateContent"
    body = exchange.request_bodies[0]
    assert body["contents"] == [{"role": "user", "parts": [{"text": _TEXT}]}]
    assert body["systemInstruction"]["parts"] == [
        {"text": "synthetic system instruction"}
    ]
    assert body["generationConfig"] == {"temperature": 0.25, "maxOutputTokens": 321}
    assert request.extensions["timeout"] == {
        "connect": 2,
        "read": 7,
        "write": 11,
        "pool": 3,
    }


@pytest.mark.asyncio
async def test_generation_decodes_text_usage_and_finish_reason(exchange):
    """実応答の本文・使用量・終了理由をSDKが失わず返す。"""
    async with _open_client() as client:
        response = await _generate(client)
    assert response.text == "generated text"
    assert response.usage_metadata.prompt_token_count == 7
    assert response.usage_metadata.candidates_token_count == 3
    assert response.candidates[0].finish_reason == types.FinishReason.STOP


@pytest.mark.asyncio
async def test_runtime_structured_output_survives_sdk_serialization(exchange):
    """runtimeのschemaが送信され、実SDKの応答を宣言済み出力型へ復元できる。"""
    exchange.respond_json(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": '{"result":"accepted","tags":["sdk"]}'}]
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    async with _open_client() as client:
        output = await GeminiAgentRuntime(client=client).call(
            make_agent(), "input", attempt_number=1
        )
    assert output == RuntimeOutput(result="accepted", tags=["sdk"])
    config = exchange.request_bodies[0]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"] == {
        "type": "OBJECT",
        "required": ["result", "tags"],
        "property_ordering": ["result", "tags"],
        "properties": {
            "result": {"type": "STRING"},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
    }


@pytest.mark.asyncio
async def test_embedder_sends_document_spec_and_decodes_vector(exchange):
    """embeddingの宣言が実要求へ渡り、実応答の値がベクトルになる。"""
    exchange.respond_json({"embeddings": [{"values": [0.1, 0.2]}]})
    async with _open_client() as client:
        embedder = GeminiEmbedder(client=client)
        vector = await embedder._call_api("synthetic document")
    assert vector == [0.1, 0.2]
    assert (
        exchange.requests[0].url.target
        == b"/v1beta/models/gemini-embedding-001:batchEmbedContents"
    )
    assert exchange.request_bodies[0]["requests"] == [
        {
            "model": "models/gemini-embedding-001",
            "content": {"parts": [{"text": "synthetic document"}], "role": "user"},
            "taskType": "RETRIEVAL_DOCUMENT",
            "outputDimensionality": embedder.SPEC.output_dimensionality,
        }
    ]


@pytest.mark.asyncio
async def test_stream_decodes_chunks_in_order_and_terminal_metadata(exchange):
    """SSEの順序と終端チャンクの使用量・終了理由を維持する。"""
    exchange.respond_sse(
        {"candidates": [{"content": {"parts": [{"text": "first "}]}}]},
        _GENERATED,
    )
    async with _open_client() as client:
        stream = await client.models.generate_content_stream(
            model=_MODEL, contents=_TEXT
        )
        chunks = [chunk async for chunk in stream]
    assert [chunk.text for chunk in chunks] == ["first ", "generated text"]
    assert chunks[-1].usage_metadata.prompt_token_count == 7
    assert chunks[-1].candidates[0].finish_reason == types.FinishReason.STOP
    assert len(exchange.requests) == 1
    assert (
        exchange.requests[0].url.target
        == b"/v1beta/models/gemini-test-model:streamGenerateContent?alt=sse"
    )


@pytest.mark.asyncio
async def test_stream_normal_completion_closes_response(exchange):
    """SSEを読み終えると応答が閉じる。"""
    exchange.respond_sse(_GENERATED)
    async with _open_client() as client:
        stream = await client.models.generate_content_stream(
            model=_MODEL, contents=_TEXT
        )
        async for _ in stream:
            pass
        assert exchange.body.closed
    assert all(client.is_closed for client in exchange.clients)


@pytest.mark.asyncio
async def test_runtime_scope_exit_closes_connection_after_early_stream_exit(
    monkeypatch,
):
    """途中終了したstreamの接続も本番runtimeの利用範囲を抜けると閉じる。"""
    first = {"candidates": [{"content": {"parts": [{"text": "first fragment"}]}}]}
    events = [
        f"data: {json.dumps(payload)}\n\n".encode() for payload in (first, _GENERATED)
    ]
    network = httpcore.AsyncMockStream(
        [
            b"HTTP/1.1 200 Connection established\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n",
            *(f"{len(event):x}\r\n".encode() + event + b"\r\n" for event in events),
            b"0\r\n\r\n",
        ]
    )
    close_connection = AsyncMock(wraps=network.aclose)
    connect = AsyncMock(return_value=network)
    monkeypatch.setattr(network, "aclose", close_connection)
    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    monkeypatch.setattr(
        "app.http.destination_resolution._resolve_host",
        AsyncMock(return_value=["93.184.216.34"]),
    )
    monkeypatch.setenv("EGRESS_PROXY_URL", "http://proxy.vector.internal:3128")
    monkeypatch.setattr(
        composition.settings, "gemini_api_key", SecretStr("synthetic-test-key")
    )

    async with composition.activate_gemini_agent_runtime() as runtime:
        stream = runtime.stream_text(
            make_agent(response_schema=None), "input", attempt_number=1
        )
        assert await anext(stream) == "first fragment"
        close_connection.assert_not_awaited()
        await stream.aclose()

    connect.assert_awaited_once()
    close_connection.assert_awaited()


@pytest.mark.asyncio
async def test_stream_read_cancellation_closes_response_and_client(exchange):
    """応答読み取りのキャンセルを保持したまま通信資源を解放する。"""
    exchange.headers = [(b"content-type", b"text/event-stream")]
    exchange.body.chunks = ()
    exchange.body.resume = asyncio.Event()

    async def consume():
        async with _open_client() as client:
            stream = GeminiAgentRuntime(client=client).stream_text(
                make_agent(response_schema=None), "input", attempt_number=1
            )
            async for _ in stream:
                pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(exchange.body.waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exchange.body.closed
        assert all(client.is_closed for client in exchange.clients)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_429_preserves_response_retry_after_and_rate_classification(
    exchange,
):
    """HTTP 429の実応答とRetry-Afterを流量制限の分類まで維持する。"""
    exchange.status = 429
    exchange.headers.append((b"retry-after", b"30"))
    exchange.respond_json(
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "synthetic rate limit",
            }
        }
    )
    async with _open_client() as client:
        with pytest.raises(errors.ClientError) as caught:
            await _generate(client)
    error = caught.value
    assert isinstance(error.response, httpx.Response)
    assert error.response.status_code == error.code == 429
    assert error.status == "RESOURCE_EXHAUSTED"
    translated = translate_gemini_error(error)
    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason == AIProviderResponseReason.RATE_LIMITED
    assert translated.http_error.retry_after == "30"
    assert len(exchange.requests) == 1


@pytest.mark.asyncio
async def test_http_429_daily_quota_details_reach_classifier(exchange):
    """実SDKが保持した日次quotaの構造化情報で利用枠の枯渇を判定する。"""
    exchange.status = 429
    exchange.respond_json(
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "synthetic quota",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}
                        ],
                    }
                ],
            }
        }
    )
    async with _open_client() as client:
        with pytest.raises(errors.ClientError) as caught:
            await _generate(client)
    translated = translate_gemini_error(caught.value)
    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason == AIProviderResponseReason.QUOTA_EXHAUSTED
    assert len(exchange.requests) == 1


@pytest.mark.asyncio
async def test_non_json_503_remains_server_failure_without_retry(exchange):
    """JSON以外の503応答も実SDKのServerErrorとして分類できる。"""
    exchange.status = 503
    exchange.headers = [(b"content-type", b"text/html")]
    exchange.body.chunks = (b"<html>synthetic unavailable</html>",)
    async with _open_client() as client:
        with pytest.raises(errors.ServerError) as caught:
            await _generate(client)
    assert isinstance(caught.value.response, httpx.Response)
    assert caught.value.response.status_code == 503
    translated = translate_gemini_error(caught.value)
    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason == AIProviderResponseReason.SERVER_ERROR
    assert len(exchange.requests) == 1


@pytest.mark.asyncio
async def test_invalid_json_response_remains_unclassified_decode_error(exchange):
    """通常応答の不正JSONは既存のJSONDecodeErrorのまま伝播する。"""
    exchange.body.chunks = (b"synthetic invalid json",)
    async with _open_client() as client:
        with pytest.raises(json.JSONDecodeError) as caught:
            await _generate(client)
    assert translate_gemini_error(caught.value) is caught.value


@pytest.mark.asyncio
async def test_invalid_sse_json_remains_unclassified_sdk_error(exchange):
    """SSEの不正JSONは原因を持つUnknownApiResponseErrorのまま伝播する。"""
    exchange.headers = [(b"content-type", b"text/event-stream")]
    exchange.body.chunks = (b"data: synthetic invalid json\n\n",)
    async with _open_client() as client:
        stream = await client.models.generate_content_stream(
            model=_MODEL, contents=_TEXT
        )
        with pytest.raises(errors.UnknownApiResponseError) as caught:
            await anext(stream)
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    assert translate_gemini_error(caught.value) is caught.value


@pytest.mark.asyncio
async def test_sse_api_error_keeps_http_status_separate_from_api_code(exchange):
    """SSE内のAPIエラーではHTTP 200とAPI code 429を別々に観測できる。"""
    exchange.respond_sse(
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "synthetic SSE error",
            }
        }
    )
    async with _open_client() as client:
        stream = await client.models.generate_content_stream(
            model=_MODEL, contents=_TEXT
        )
        with pytest.raises(errors.ClientError) as caught:
            await anext(stream)
    assert caught.value.code == 429
    assert caught.value.response.status_code == 200
    assert caught.value.status == "RESOURCE_EXHAUSTED"
    assert len(exchange.requests) == 1


@pytest.mark.asyncio
async def test_timeout_object_survives_sdk_without_retry(exchange):
    """受信timeoutをSDKが包み直したり再試行したりしない。"""
    failure = httpx.ReadTimeout("synthetic timeout")
    exchange.error = failure
    async with _open_client() as client:
        with pytest.raises(httpx.ReadTimeout) as caught:
            await _generate(client)
    assert caught.value is failure
    assert len(exchange.requests) == 1
    assert all(client.is_closed for client in exchange.clients)


@pytest.mark.asyncio
async def test_connection_error_keeps_original_cause_without_retry(exchange):
    """接続失敗の元の例外と原因をSDKが保持する。"""
    cause = OSError("synthetic connection failure")
    failure = httpx.ConnectError("synthetic connect error")
    failure.__cause__ = cause
    exchange.error = failure
    async with _open_client() as client:
        with pytest.raises(httpx.ConnectError) as caught:
            await _generate(client)
    assert caught.value is failure
    assert caught.value.__cause__ is cause
    assert len(exchange.requests) == 1
