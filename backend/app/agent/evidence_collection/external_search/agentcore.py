"""External search gatewayとしてAgentCore Gatewayのweb-search toolを呼ぶadapter。

宛先は自 AWS アカウントに作った resource なので、内部宛 client を使い SigV4 で署名する。
backend で唯一の「ヘッダ署名」実装 (``app/redis/iam_auth.py`` は presigned URL、
``app/db_iam_auth.py`` は generate_db_auth_token)。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any, Final, Protocol, cast
from urllib.parse import urlparse

import httpx
import logfire
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import ValidationError

from app.agent.evidence_collection.external_search.agentcore_spec import (
    AGENTCORE_WEB_SEARCH_SPEC,
    AgentCoreWebSearchSpec,
    build_tool_arguments,
    build_tool_call_payload,
)
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_CONTENT_MAX_CHARS,
    ExternalSearchFailureReason,
    ExternalSearchHit,
    ExternalSearchProviderError,
    ExternalSearchRequest,
)
from app.agent.evidence_collection.external_search.metrics import (
    record_external_hit_dropped,
    record_external_hit_truncated,
)
from app.shared.security.safe_url import SafeUrl

__all__ = [
    "AgentCoreWebSearchGateway",
]

_SPAN_NAME: Final[str] = "external_search_call"
_SIGNING_SERVICE: Final[str] = "bedrock-agentcore"
_MISSING_HITS = object()

# publishedDate は "07:45AM, Thursday, August 27 2026, PDT" 形 (probe 2026-08-30、
# 30/30 一致)。時刻と TZ 略号は捨てて日付だけ取る: 略号は標準ライブラリで解決できず、
# 誤って解くと表示日が発行元の主張とずれる。月名は表で引く (strptime の %B は
# LC_TIME に依存するため使わない)。
_PUBLISHED_DATE_PATTERN: Final = re.compile(
    r"\b(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\b"
)
_MONTHS: Final[Mapping[str, int]] = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


class AgentCoreHttpClient(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
    ) -> httpx.Response: ...


class AgentCoreWebSearchGateway:
    """完成済みqueryをAgentCore Gateway経由で実行し、検証済みhitへ変換する。"""

    SPEC: Final[AgentCoreWebSearchSpec] = AGENTCORE_WEB_SEARCH_SPEC

    __slots__ = ("_client", "_mcp_url", "_region")

    def __init__(
        self,
        *,
        gateway_url: str,
        region: str,
        client: AgentCoreHttpClient,
    ) -> None:
        if not gateway_url:
            raise ValueError("AGENTCORE_GATEWAY_URL is not configured")
        if not region:
            raise ValueError("AWS_REGION is not configured")
        self._mcp_url = f"{gateway_url.rstrip('/')}{self.SPEC.mcp_path}"
        self._region = region
        self._client = client

    async def search(
        self,
        request: ExternalSearchRequest,
    ) -> list[ExternalSearchHit]:
        if request.limit <= 0:
            raise ValueError("limit must be greater than 0")

        classified_error: ExternalSearchProviderError | None = None
        hits: list[ExternalSearchHit] | object = _MISSING_HITS
        with logfire.span(
            _SPAN_NAME,
            _span_kind=SpanKind.CLIENT,
        ) as span:
            try:
                hits = await self._execute(request)
            except ExternalSearchProviderError as exc:
                classified_error = exc
                span.set_attribute(ERROR_TYPE, exc.reason)
                span.set_status(StatusCode.ERROR)
            else:
                span.set_attribute("hit_count", len(hits))

        if classified_error is not None:
            raise classified_error
        if hits is _MISSING_HITS:
            raise RuntimeError("AgentCore search completed without hits")
        return cast(list[ExternalSearchHit], hits)

    async def _execute(
        self,
        request: ExternalSearchRequest,
    ) -> list[ExternalSearchHit]:
        response = await self._post_tool_call(request)
        results = _results_from_response(response)

        hits: list[ExternalSearchHit] = []
        for result in results:
            hit = _hit_from_result(result)
            if hit is not None:
                hits.append(hit)
        return hits[: request.limit]

    async def _post_tool_call(self, request: ExternalSearchRequest) -> httpx.Response:
        payload = build_tool_call_payload(
            build_tool_arguments(
                self.SPEC,
                query=request.query,
                limit=request.limit,
                date_filter=request.date_filter,
            ),
            name=self.SPEC.tool_name,
        )
        # 署名は body のバイト列に対して行うので、送信する側と同じ bytes を使う。
        body = json.dumps(payload).encode()
        headers = await _signed_headers(
            url=self._mcp_url, body=body, region=self._region
        )

        transport_failure: ExternalSearchFailureReason | None = None
        try:
            response = await self._client.post(
                self._mcp_url, headers=headers, content=body
            )
        except httpx.RequestError:
            transport_failure = ExternalSearchFailureReason.HTTP_ERROR

        if transport_failure is not None:
            raise ExternalSearchProviderError(reason=transport_failure)

        if not 200 <= response.status_code < 300:
            raise ExternalSearchProviderError(
                reason=ExternalSearchFailureReason.HTTP_STATUS,
                status_code=response.status_code,
            )
        return response


@lru_cache(maxsize=1)
def _botocore_session() -> Any:
    """プロセス内で 1 本だけ持つ botocore session。

    session ごとに credential を独立に取得・更新するため、1 本に共有する
    (``app/redis/iam_auth.py`` の同名関数と同じ判断)。botocore の import と
    credential 解決 (ECS では HTTP) も初回の署名まで遅れる。
    """
    import botocore.session

    return botocore.session.get_session()


def _sign(*, url: str, body: bytes, region: str) -> dict[str, str]:
    """SigV4 署名済みヘッダを作る (同期)。"""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    aws_request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            # Gateway は JSON と SSE のどちらでも返しうるので両方受ける。
            "Accept": "application/json, text/event-stream",
        },
    )
    credentials = _botocore_session().get_credentials()
    if credentials is None:
        raise ExternalSearchProviderError(
            reason=ExternalSearchFailureReason.CREDENTIALS_UNAVAILABLE
        )
    SigV4Auth(credentials.get_frozen_credentials(), _SIGNING_SERVICE, region).add_auth(
        aws_request
    )
    return dict(aws_request.headers)


async def _signed_headers(*, url: str, body: bytes, region: str) -> dict[str, str]:
    """署名を thread へ逃がす。

    資格情報の解決は初回に同期 I/O (ECS では container credentials endpoint への
    HTTP) になるため、event loop を止めない (``app/db_iam_auth.py`` と同じ)。
    """
    from botocore.exceptions import BotoCoreError

    try:
        return await asyncio.to_thread(_sign, url=url, body=body, region=region)
    except BotoCoreError as exc:
        raise ExternalSearchProviderError(
            reason=ExternalSearchFailureReason.CREDENTIALS_UNAVAILABLE
        ) from exc


def _results_from_response(response: httpx.Response) -> list[object]:
    """二重 JSON を解いて results を取り出す。

    MCP は tool の出力を ``result.content[0].text`` に **JSON 文字列として**
    載せるので、parse が 2 回要る (probe 2026-08-30)。
    """
    try:
        envelope = response.json()
    except ValueError:
        envelope = None
    if not isinstance(envelope, Mapping):
        raise ExternalSearchProviderError(
            reason=ExternalSearchFailureReason.INVALID_JSON
        )
    # JSON-RPC の error と tool 側の isError は別経路の失敗だが、呼び出し側から
    # できることは同じなので 1 つの reason に畳む。中身は provider の自由文なので
    # 載せない。
    result = envelope.get("result")
    if "error" in envelope or (
        isinstance(result, Mapping) and result.get("isError") is True
    ):
        raise ExternalSearchProviderError(reason=ExternalSearchFailureReason.MCP_ERROR)

    payload = _inner_payload(result)
    results = payload.get("results")
    if not isinstance(results, list):
        raise ExternalSearchProviderError(
            reason=ExternalSearchFailureReason.INVALID_RESULTS
        )
    return results


def _inner_payload(result: object) -> Mapping[str, Any]:
    if not isinstance(result, Mapping):
        raise ExternalSearchProviderError(
            reason=ExternalSearchFailureReason.INVALID_RESULTS
        )
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise ExternalSearchProviderError(
            reason=ExternalSearchFailureReason.INVALID_RESULTS
        )
    first = content[0]
    text = first.get("text") if isinstance(first, Mapping) else None
    if not isinstance(text, str):
        raise ExternalSearchProviderError(
            reason=ExternalSearchFailureReason.INVALID_RESULTS
        )
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if not isinstance(payload, Mapping):
        raise ExternalSearchProviderError(
            reason=ExternalSearchFailureReason.INVALID_RESULTS
        )
    return payload


def _hit_from_result(result: object) -> ExternalSearchHit | None:
    if not isinstance(result, Mapping):
        record_external_hit_dropped(reason="result_not_mapping")
        return None

    title = _clean_required_text(result.get("title"))
    if title is None:
        record_external_hit_dropped(reason="title_missing")
        return None

    url = _safe_url(result.get("url"))
    if url is None:
        record_external_hit_dropped(reason="url_unsafe")
        return None

    return ExternalSearchHit(
        url=url,
        title=title,
        content=_clipped_content(result.get("text")),
        published_at=_parse_published_date(result.get("publishedDate")),
        source_name=_source_name(url.root),
    )


def _clean_required_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _clipped_content(value: object) -> str | None:
    """本文をhitが運べる長さへ切り詰める。

    provider が返すのは snippet ではなくページ本文の抜粋 (実測 3.1k〜4.3k、
    probe 2026-08-30) なので、長さは契約違反のシグナルにならない。落とさず削る。
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) <= EXTERNAL_CONTENT_MAX_CHARS:
        return value
    record_external_hit_truncated()
    return value[:EXTERNAL_CONTENT_MAX_CHARS]


def _safe_url(value: object) -> SafeUrl | None:
    try:
        return SafeUrl.model_validate(value)
    except ValidationError:
        return None


def _parse_published_date(value: object) -> datetime | None:
    """公開日を日精度で取る。時刻とTZ略号は使わない。

    値は "07:45AM, Thursday, August 27 2026, PDT" 形。TZ 略号は曖昧で
    (CST は米中部/中国/キューバ)、``datetime`` も解釈できない。誤った instant を
    持つより、発行元が主張する日付をそのまま残す方が表示にも鮮度判断にも忠実。
    """
    if not isinstance(value, str):
        return None
    match = _PUBLISHED_DATE_PATTERN.search(value)
    if match is None:
        return None
    month = _MONTHS.get(match["month"])
    if month is None:
        return None
    try:
        published = date(int(match["year"]), month, int(match["day"]))
    except ValueError:
        return None
    return datetime.combine(published, datetime.min.time(), tzinfo=UTC)


def _source_name(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    host = urlparse(url.strip()).hostname
    if host is None:
        return None
    if host.startswith("www."):
        return host[4:]
    return host
