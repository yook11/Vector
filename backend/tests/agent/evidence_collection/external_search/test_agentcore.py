"""AgentCore Web Search adapter tests。

実機の値は probe 2026-08-30 に基づく。録画レスポンスを使うテストは、期待値を
標本から導出して「仕様が壊れた」と「標本が変わった」を区別できるようにする。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from botocore.exceptions import NoCredentialsError
from logfire.testing import CaptureLogfire

import app.agent.evidence_collection.external_search.agentcore as agentcore_module
from app.agent.evidence_collection.external_search import (
    AGENTCORE_WEB_SEARCH_SPEC,
    EXTERNAL_CONTENT_MAX_CHARS,
    AgentCoreWebSearchGateway,
    ExternalSearchDateFilter,
    ExternalSearchProviderError,
    ExternalSearchRequest,
)
from app.agent.evidence_collection.external_search.metrics import ExternalHitDropReason
from tests.logfire._metric_helpers import assert_attribute_contract, collected_metrics

GATEWAY_URL = "https://gw-test.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com"
MCP_URL = f"{GATEWAY_URL}/mcp"
REGION = "ap-northeast-1"
ACCESS_KEY_SENTINEL = "AKIAACCESSKEYSENTINEL"
# 署名に渡す偽の資格情報。実在する鍵に見える形にすると secret scan に引っかかる。
FAKE_SIGNING_SECRET = "fake-signing-secret"
# provider 応答本文に混ぜて、error message へ漏れないことを見るための目印。
RESPONSE_BODY_SENTINEL = "PROVIDER_BODY_SENTINEL"

_HIT_DROPPED_METRIC = "vector.agent.external_search.hit_dropped"
_HIT_TRUNCATED_METRIC = "vector.agent.external_search.hit_truncated"
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"
_FIXTURE = "agentcore_web_search.json"


@pytest.fixture(autouse=True)
def _static_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """署名は本物の SigV4Auth を通し、資格情報だけ固定値に差し替える。

    ECS の container credentials endpoint へ出ないようにするためで、署名処理自体を
    fake にすると「送信するのと同じ body に署名しているか」が検証できなくなる。
    """
    from botocore.credentials import Credentials

    class _Session:
        def get_credentials(self) -> Credentials:
            return Credentials(ACCESS_KEY_SENTINEL, FAKE_SIGNING_SECRET)

    monkeypatch.setattr(agentcore_module, "_botocore_session", _Session)


def _drop_counts_by_reason(metrics: list[dict[str, Any]]) -> dict[str, int]:
    """drop counterのreason別加算値を取り出す(期待値は持たない)。"""
    metric = next(m for m in metrics if m["name"] == _HIT_DROPPED_METRIC)
    return {
        str(point["attributes"]["reason"]): int(point["value"])
        for point in metric["data"]["data_points"]
    }


def _truncated_count(metrics: list[dict[str, Any]]) -> int:
    metric = next(
        (m for m in metrics if m["name"] == _HIT_TRUNCATED_METRIC),
        None,
    )
    if metric is None:
        return 0
    return sum(int(point["value"]) for point in metric["data"]["data_points"])


def _gateway(client: object) -> AgentCoreWebSearchGateway:
    return AgentCoreWebSearchGateway(
        gateway_url=GATEWAY_URL,
        region=REGION,
        client=client,  # type: ignore[arg-type]
    )


async def _search(
    gateway: AgentCoreWebSearchGateway,
    *,
    query: str,
    limit: int,
    date_filter: ExternalSearchDateFilter | None = None,
) -> list[Any]:
    return await gateway.search(
        ExternalSearchRequest(query=query, limit=limit, date_filter=date_filter)
    )


def _result(
    *,
    url: str = "https://www.example.com/news",
    title: str = "Example title",
    text: object | None = "Example body",
    published_date: object | None = "07:45AM, Thursday, August 27 2026, PDT",
) -> dict[str, object]:
    result: dict[str, object] = {"url": url, "title": title}
    if text is not None:
        result["text"] = text
    if published_date is not None:
        result["publishedDate"] = published_date
    return result


def _envelope(inner: object, *, is_error: bool = False) -> dict[str, object]:
    """MCP は tool 出力を content[0].text に JSON 文字列として載せる (二重 JSON)。"""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": is_error,
            "content": [{"type": "text", "text": json.dumps(inner)}],
        },
    }


def _response(results: list[object], *, is_error: bool = False) -> httpx.Response:
    return httpx.Response(
        200, json=_envelope({"id": "probe", "results": results}, is_error=is_error)
    )


def _mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _StubClient:
    """post の返り値だけを決める最小の client。

    outcome が例外なら raise する。httpx.MockTransport では表現できない
    「transport より手前で失敗する」ケース用。
    """

    def __init__(self, outcome: httpx.Response | BaseException) -> None:
        self._outcome = outcome
        self.calls: list[tuple[str, dict[str, str], bytes]] = []

    async def post(
        self, url: str, *, headers: dict[str, str], content: bytes
    ) -> httpx.Response:
        self.calls.append((url, dict(headers), content))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


# --- リクエストの形 ---


@pytest.mark.asyncio
async def test_search_posts_signed_mcp_tool_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response([_result()])

    async with _mock_client(handler) as client:
        await _search(_gateway(client), query="NVIDIA Blackwell", limit=3)

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == MCP_URL
    assert json.loads(request.content) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web-search___WebSearch",
            "arguments": {"query": "NVIDIA Blackwell", "maxResults": 3},
        },
    }
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json, text/event-stream"
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert f"Credential={ACCESS_KEY_SENTINEL}/" in request.headers["authorization"]
    assert "x-amz-date" in request.headers


@pytest.mark.asyncio
async def test_signature_covers_the_body_and_url_that_are_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """署名対象と送信内容が同一であること。

    SigV4 は body と URL を canonical request に含めるので、ずれると AWS 側で
    SignatureDoesNotMatch になる。ヘッダには payload hash が載らない
    (x-amz-content-sha256 は S3 用) ため、署名関数が受けた値を直接突き合わせる。
    """
    signed: list[tuple[str, bytes]] = []
    original = agentcore_module._sign

    def spy(*, url: str, body: bytes, region: str) -> dict[str, str]:
        signed.append((url, body))
        return original(url=url, body=body, region=region)

    monkeypatch.setattr(agentcore_module, "_sign", spy)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response([])

    async with _mock_client(handler) as client:
        await _search(_gateway(client), query="query", limit=1)

    assert len(signed) == 1
    assert signed[0] == (str(requests[0].url), requests[0].content)


@pytest.mark.parametrize(
    ("date_filter", "expected_from", "expected_to"),
    [
        pytest.param(
            ExternalSearchDateFilter(
                start_date=date(2026, 6, 1), end_date=date(2026, 6, 16)
            ),
            "2026-05-31",
            "2026-06-15",
            id="ordinary-range",
        ),
        pytest.param(
            ExternalSearchDateFilter(
                start_date=date(2026, 1, 1), end_date=date(2026, 2, 1)
            ),
            "2025-12-31",
            "2026-01-31",
            id="crosses-year-boundary",
        ),
        pytest.param(
            ExternalSearchDateFilter(
                start_date=date(2026, 8, 30), end_date=date(2026, 8, 31)
            ),
            "2026-08-29",
            "2026-08-30",
            id="single-day",
        ),
    ],
)
@pytest.mark.asyncio
async def test_search_maps_half_open_range_to_inclusive_published_date_filter(
    date_filter: ExternalSearchDateFilter,
    expected_from: str,
    expected_to: str,
) -> None:
    """半開区間を両端 inclusive の filter へ写す。

    publishedDateFilter は両端 inclusive / UTC (inputSchema、probe 2026-08-30)
    なので末尾は end_date の前日。先頭を 1 日広げるのは、期間が JST で解決される
    のに filter が UTC で、開始日の JST 午前が漏れるため。
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response([])

    async with _mock_client(handler) as client:
        await _search(_gateway(client), query="q", limit=5, date_filter=date_filter)

    arguments = json.loads(requests[0].content)["params"]["arguments"]
    assert arguments["filters"] == {
        "publishedDateFilter": {"from": expected_from, "to": expected_to}
    }


@pytest.mark.asyncio
async def test_search_clamps_max_results_to_provider_limit() -> None:
    """inputSchema の上限 (1-25、probe 2026-08-30) を超えて要求しない。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response([])

    async with _mock_client(handler) as client:
        await _search(
            _gateway(client),
            query="q",
            limit=AGENTCORE_WEB_SEARCH_SPEC.max_results_limit + 5,
        )

    arguments = json.loads(requests[0].content)["params"]["arguments"]
    assert arguments["maxResults"] == AGENTCORE_WEB_SEARCH_SPEC.max_results_limit


@pytest.mark.asyncio
async def test_search_rejects_non_positive_limit() -> None:
    async with _mock_client(lambda _: _response([])) as client:
        with pytest.raises(ValueError):
            await _search(_gateway(client), query="q", limit=0)


def test_gateway_requires_url_and_region() -> None:
    with pytest.raises(ValueError):
        AgentCoreWebSearchGateway(gateway_url="", region=REGION, client=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AgentCoreWebSearchGateway(gateway_url=GATEWAY_URL, region="", client=object())  # type: ignore[arg-type]


# --- 失敗の分類 ---


@pytest.mark.parametrize("status_code", [400, 403, 429, 500])
@pytest.mark.asyncio
async def test_search_wraps_non_2xx_without_leaking_body_or_credentials(
    status_code: int,
) -> None:
    body = {"message": f"body mentions {RESPONSE_BODY_SENTINEL}"}

    async with _mock_client(lambda _: httpx.Response(status_code, json=body)) as client:
        with pytest.raises(ExternalSearchProviderError) as raised:
            await _search(_gateway(client), query="q", limit=1)

    message = str(raised.value)
    assert raised.value.reason == f"external_search_http_status_{status_code}"
    assert str(status_code) in message
    assert RESPONSE_BODY_SENTINEL not in message
    assert FAKE_SIGNING_SECRET not in message
    assert "body mentions" not in message


@pytest.mark.asyncio
async def test_search_classifies_transport_failure_as_http_error() -> None:
    client = _StubClient(httpx.ConnectError("boom"))

    with pytest.raises(ExternalSearchProviderError) as raised:
        await _search(_gateway(client), query="q", limit=1)

    assert raised.value.reason == "external_search_http_error"


@pytest.mark.asyncio
async def test_search_classifies_client_timeout_as_http_error() -> None:
    """内側の timeout は分類済み reason として span に残る。

    service の wait_for で外側から切られると provider_failed しか残らないため、
    client 側の timeout を先に効かせる契約になっている。
    """
    client = _StubClient(httpx.ReadTimeout("slow"))

    with pytest.raises(ExternalSearchProviderError) as raised:
        await _search(_gateway(client), query="q", limit=1)

    assert raised.value.reason == "external_search_http_error"


@pytest.mark.asyncio
async def test_search_classifies_non_json_body_as_invalid_json() -> None:
    async with _mock_client(lambda _: httpx.Response(200, text="not json")) as client:
        with pytest.raises(ExternalSearchProviderError) as raised:
            await _search(_gateway(client), query="q", limit=1)

    assert raised.value.reason == "external_search_invalid_json"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "boom"}},
            id="json-rpc-error",
        ),
        pytest.param(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": '{"results": []}'}],
                },
            },
            id="tool-is-error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_search_classifies_mcp_level_failure_as_mcp_error(
    payload: dict[str, object],
) -> None:
    """HTTP 200 のまま返る失敗を 1 つの reason に畳む。

    JSON-RPC の error と tool の isError は経路が違うが、呼び出し側から
    できることは同じ。provider の自由文は reason に載せない。
    """
    async with _mock_client(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(ExternalSearchProviderError) as raised:
            await _search(_gateway(client), query="q", limit=1)

    assert raised.value.reason == "external_search_mcp_error"
    assert "boom" not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"jsonrpc": "2.0", "id": 1}, id="result-missing"),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "result": {"content": []}},
            id="content-empty",
        ),
        pytest.param(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "not json"}]},
            },
            id="inner-not-json",
        ),
        pytest.param(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": '{"results": "x"}'}]},
            },
            id="results-not-a-list",
        ),
    ],
)
@pytest.mark.asyncio
async def test_search_classifies_malformed_payload_as_invalid_results(
    payload: dict[str, object],
) -> None:
    async with _mock_client(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(ExternalSearchProviderError) as raised:
            await _search(_gateway(client), query="q", limit=1)

    assert raised.value.reason == "external_search_invalid_results"


@pytest.mark.asyncio
async def test_search_classifies_credential_failure_without_reaching_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """資格情報が解けないのは provider 障害ではなく実行環境側。

    分類を落とすと botocore の例外が素通りして run 全体の evidence collection を
    落とす (service は ExternalSearchProviderError と TimeoutError しか catch しない)。
    """

    class _Session:
        def get_credentials(self) -> object:
            raise NoCredentialsError()

    monkeypatch.setattr(agentcore_module, "_botocore_session", _Session)
    client = _StubClient(_response([]))

    with pytest.raises(ExternalSearchProviderError) as raised:
        await _search(_gateway(client), query="q", limit=1)

    assert raised.value.reason == "external_search_credentials_unavailable"
    assert client.calls == []


@pytest.mark.asyncio
async def test_search_classifies_absent_credentials_as_credentials_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        def get_credentials(self) -> None:
            return None

    monkeypatch.setattr(agentcore_module, "_botocore_session", _Session)
    client = _StubClient(_response([]))

    with pytest.raises(ExternalSearchProviderError) as raised:
        await _search(_gateway(client), query="q", limit=1)

    assert raised.value.reason == "external_search_credentials_unavailable"
    assert client.calls == []


# --- hit への変換 ---


@pytest.mark.asyncio
async def test_search_records_a_drop_metric_for_every_intake_rejection(
    capfire: CaptureLogfire,
) -> None:
    """収集段で落とした1件ごとに、理由ラベル付きでcounterが1つ増える。"""
    payload = [
        "not-a-mapping",
        _result(url="https://example.com/empty-title", title="  "),
        _result(url="http://169.254.169.254/news", title="Private IP"),
        _result(url="https://example.com/valid", title="Valid"),
    ]

    async with _mock_client(lambda _: _response(payload)) as client:
        hits = await _search(_gateway(client), query="q", limit=10)

    metrics = collected_metrics(capfire)
    assert [hit.title for hit in hits] == ["Valid"]
    assert_attribute_contract(
        metrics,
        _HIT_DROPPED_METRIC,
        allowed={"reason": set(get_args(ExternalHitDropReason))},
    )
    assert _drop_counts_by_reason(metrics) == {
        "result_not_mapping": 1,
        "title_missing": 1,
        "url_unsafe": 1,
    }


@pytest.mark.asyncio
async def test_search_truncates_long_body_instead_of_dropping_the_hit(
    capfire: CaptureLogfire,
) -> None:
    """本文はページ抜粋なので長さは契約違反のシグナルにならない。

    実測 3.1k〜4.3k (probe 2026-08-30) で、上限は 1/3 の hit に効く。落とすと
    出典そのものが消えるため、削って残す。
    """
    oversized = "x" * (EXTERNAL_CONTENT_MAX_CHARS + 100)

    async with _mock_client(lambda _: _response([_result(text=oversized)])) as client:
        hits = await _search(_gateway(client), query="q", limit=10)

    metrics = collected_metrics(capfire)
    assert len(hits) == 1
    assert hits[0].content is not None
    assert len(hits[0].content) == EXTERNAL_CONTENT_MAX_CHARS
    assert _truncated_count(metrics) == 1


@pytest.mark.asyncio
async def test_search_does_not_count_truncation_when_body_fits(
    capfire: CaptureLogfire,
) -> None:
    fitting = "x" * EXTERNAL_CONTENT_MAX_CHARS

    async with _mock_client(lambda _: _response([_result(text=fitting)])) as client:
        hits = await _search(_gateway(client), query="q", limit=10)

    assert len(hits[0].content or "") == EXTERNAL_CONTENT_MAX_CHARS
    assert _truncated_count(collected_metrics(capfire)) == 0


@pytest.mark.asyncio
async def test_search_truncates_hits_to_requested_limit() -> None:
    results = [_result(url=f"https://example.com/{i}") for i in range(5)]

    async with _mock_client(lambda _: _response(results)) as client:
        hits = await _search(_gateway(client), query="q", limit=2)

    assert len(hits) == 2


@pytest.mark.asyncio
async def test_search_derives_source_name_from_host_without_www() -> None:
    async with _mock_client(
        lambda _: _response([_result(url="https://www.example.com/a")])
    ) as client:
        hits = await _search(_gateway(client), query="q", limit=1)

    assert hits[0].source_name == "example.com"


@pytest.mark.parametrize(
    ("published_date", "expected"),
    [
        pytest.param(
            "07:45AM, Thursday, August 27 2026, PDT",
            datetime(2026, 8, 27, tzinfo=UTC),
            id="observed-format",
        ),
        pytest.param(
            "05:00PM, Tuesday, August 25 2026, PDT",
            datetime(2026, 8, 25, tzinfo=UTC),
            # 17:00 PDT は JST では翌日だが、発行元が主張する日付を残す。
            id="evening-keeps-publisher-date",
        ),
        pytest.param(
            "06:49AM, Monday, August 03 2026, PDT",
            datetime(2026, 8, 3, tzinfo=UTC),
            id="zero-padded-day",
        ),
        pytest.param("2026-07-04T12:30:00Z", None, id="iso-8601-is-not-the-contract"),
        pytest.param("Augustus 27 2026", None, id="unknown-month-name"),
        pytest.param(
            "02:59AM, Tuesday, Februrary 30 2026, PST", None, id="impossible-date"
        ),
        pytest.param("", None, id="empty"),
        pytest.param(None, None, id="absent"),
        pytest.param(12345, None, id="not-a-string"),
    ],
)
@pytest.mark.asyncio
async def test_search_keeps_publication_date_at_day_precision(
    published_date: object | None,
    expected: datetime | None,
) -> None:
    """時刻と TZ 略号は使わない。

    略号は曖昧 (CST は米中部/中国/キューバ) で datetime も解釈できないため、
    誤った instant を持つより発行元の日付をそのまま残す。
    """
    async with _mock_client(
        lambda _: _response([_result(published_date=published_date)])
    ) as client:
        hits = await _search(_gateway(client), query="q", limit=1)

    assert hits[0].published_at == expected


# --- 録画した実レスポンス ---


def _recorded_results() -> list[dict[str, Any]]:
    """録画 payload の results (期待値を標本から導出するため)。"""
    outer = json.loads((_FIXTURES_DIR / _FIXTURE).read_text())
    inner = json.loads(outer["result"]["content"][0]["text"])
    return list(inner["results"])


@pytest.mark.asyncio
async def test_recorded_response_maps_every_result_to_a_hit() -> None:
    """録画した実バイトで adapter を通し、1 件も落ちないことを固定する。

    件数は標本から導出する。fixture を録り直したときに「仕様が壊れた」と
    「標本が変わった」を区別するため。
    """
    raw = (_FIXTURES_DIR / _FIXTURE).read_bytes()

    async with _mock_client(lambda _: httpx.Response(200, content=raw)) as client:
        hits = await _search(_gateway(client), query="q", limit=25)

    recorded = _recorded_results()
    assert len(hits) == len(recorded)
    assert [hit.title for hit in hits] == [r["title"] for r in recorded]
    assert [str(hit.url) for hit in hits] == [r["url"] for r in recorded]


@pytest.mark.asyncio
async def test_recorded_response_yields_a_date_for_every_result() -> None:
    """実値の publishedDate が全件パースできること (書式は probe で 30/30 一致)。"""
    raw = (_FIXTURES_DIR / _FIXTURE).read_bytes()

    async with _mock_client(lambda _: httpx.Response(200, content=raw)) as client:
        hits = await _search(_gateway(client), query="q", limit=25)

    assert all(hit.published_at is not None for hit in hits)
    assert all(
        hit.published_at is not None and hit.published_at.tzinfo is UTC for hit in hits
    )


@pytest.mark.asyncio
async def test_recorded_response_truncates_only_the_oversized_results(
    capfire: CaptureLogfire,
) -> None:
    """切り詰めの発生件数を標本から導出する。"""
    raw = (_FIXTURES_DIR / _FIXTURE).read_bytes()

    async with _mock_client(lambda _: httpx.Response(200, content=raw)) as client:
        hits = await _search(_gateway(client), query="q", limit=25)

    expected_truncations = sum(
        1 for r in _recorded_results() if len(r["text"]) > EXTERNAL_CONTENT_MAX_CHARS
    )
    assert expected_truncations > 0  # 標本が切り詰めを含まないとこのテストは空虚
    assert _truncated_count(collected_metrics(capfire)) == expected_truncations
    assert all(len(hit.content or "") <= EXTERNAL_CONTENT_MAX_CHARS for hit in hits)
