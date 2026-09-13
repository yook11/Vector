"""robotsの確認と制限付きの記事受信を行う新経路の取得境界。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.collection.article_completion.content import RawResponse
from app.collection.article_completion.errors import (
    FetchDeadlineExceededError,
    FetchResource,
    ResponseSizeBasis,
    ResponseSizeLimitExceededError,
    RobotsDisallowedError,
)
from app.http.destination_resolution import HostResolutionError
from app.http.error_mapping import (
    http_response_error_from_exception,
    http_transport_error_from_exception,
)
from app.http.external import make_external_async_client
from app.shared.security.safe_url import SafeUrl

_USER_AGENT = "VectorBot/1.0 (+https://github.com/vector-news)"
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_ROBOTS_TIMEOUT_SECONDS = 10.0
_ARTICLE_TIMEOUT_SECONDS = 30.0


@asynccontextmanager
async def _open_response(
    client: httpx.AsyncClient,  # noqa: TID251
    url: str,
    *,
    resource: FetchResource,
    timeout_seconds: float,
) -> AsyncIterator[httpx.Response]:
    """応答待ちと本文受信に共通の期限をかけ、通信の事実を変換する。"""
    deadline = asyncio.timeout(timeout_seconds)
    try:
        async with deadline:
            try:
                async with client.stream(
                    "GET", url, timeout=timeout_seconds, follow_redirects=False
                ) as response:
                    received_at = datetime.now(UTC)
                    if not (
                        resource == FetchResource.ROBOTS_TXT
                        and response.status_code == 404
                    ):
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise http_response_error_from_exception(
                                exc, received_at=received_at
                            ) from exc
                    yield response
            except (httpx.HTTPError, HostResolutionError) as exc:
                mapped = http_transport_error_from_exception(exc)
                if mapped is None:
                    raise
                raise mapped from exc
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise FetchDeadlineExceededError(
            resource=resource, limit_seconds=timeout_seconds
        ) from exc


async def _read_body(response: httpx.Response, *, resource: FetchResource) -> bytes:
    """上限を超えたチャンクを保持せず、展開後の本文量を制限する。"""
    content_length = response.headers.get("content-length")
    if content_length is not None:
        value = content_length.strip()
        if value.isascii() and value.isdecimal():
            try:
                declared_bytes = int(value)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > _MAX_RESPONSE_BYTES:
                raise ResponseSizeLimitExceededError(
                    resource=resource,
                    limit_bytes=_MAX_RESPONSE_BYTES,
                    observed_bytes=declared_bytes,
                    size_basis=ResponseSizeBasis.DECLARED_CONTENT_LENGTH,
                )

    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_CHUNK_BYTES):
        observed_bytes = len(body) + len(chunk)
        if observed_bytes > _MAX_RESPONSE_BYTES:
            raise ResponseSizeLimitExceededError(
                resource=resource,
                limit_bytes=_MAX_RESPONSE_BYTES,
                observed_bytes=observed_bytes,
                size_basis=ResponseSizeBasis.RECEIVED_DECODED_BODY,
            )
        body.extend(chunk)
    return bytes(body)


async def fetch_article_response(url: SafeUrl) -> RawResponse:
    """取得ルールを確認し、記事の応答を抽出処理へ渡せる素材として返す。"""
    article_url = str(url)
    parsed = urlsplit(article_url)
    robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))

    async with make_external_async_client(
        headers={"User-Agent": _USER_AGENT}, follow_redirects=False
    ) as client:
        async with _open_response(
            client,
            robots_url,
            resource=FetchResource.ROBOTS_TXT,
            timeout_seconds=_ROBOTS_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code == 404:
                robots_text = None
            else:
                body = await _read_body(response, resource=FetchResource.ROBOTS_TXT)
                robots_text = body.decode(
                    response.encoding or "utf-8", errors="replace"
                )

        if robots_text is not None:
            rules = RobotFileParser()
            rules.parse(robots_text.splitlines())
            if not rules.can_fetch(_USER_AGENT, article_url):
                raise RobotsDisallowedError()

        async with _open_response(
            client,
            article_url,
            resource=FetchResource.ARTICLE_PAGE,
            timeout_seconds=_ARTICLE_TIMEOUT_SECONDS,
        ) as response:
            body = await _read_body(response, resource=FetchResource.ARTICLE_PAGE)
            return RawResponse(
                url=str(response.url),
                content_type=response.headers.get("content-type"),
                charset_from_header=response.charset_encoding,
                content=body,
                decoded_text=body.decode(
                    response.encoding or "utf-8", errors="replace"
                ),
            )
