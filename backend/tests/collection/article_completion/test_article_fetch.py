"""新しい記事取得の許可判定・受信制限・失敗境界を検証する。"""

import asyncio
import gzip
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.collection.article_completion import article_fetch
from app.collection.article_completion.content import RawResponse
from app.collection.article_completion.errors import (
    FetchDeadlineExceededError,
    FetchResource,
    ResponseSizeBasis,
    ResponseSizeLimitExceededError,
    RobotsDisallowedError,
)
from app.http import external
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import HttpTransportFailureReason
from app.shared.security.safe_url import SafeUrl

_URL = SafeUrl("https://example.com:8443/news/article?edition=1")


class BodyStream(httpx.AsyncByteStream):
    """読み込まれたチャンクと解放を観測できる応答本文。"""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class DripStream(BodyStream):
    """通信は続いているが完了しない応答本文。"""

    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.started.set()
            self.read_count += 1
            yield b"x"
            await asyncio.sleep(0.001)


@pytest.fixture
def install_client(monkeypatch):
    """実HTTPXの応答処理を使い、送信先だけをテスト内で制御する。"""

    def install(handler):
        clients = []

        def factory(**kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr(article_fetch, "make_external_async_client", factory)
        return clients

    return install


@pytest.mark.parametrize(
    ("robots_status", "robots_body", "content_type", "body", "charset", "text"),
    [
        (
            200,
            b"User-agent: *\nAllow: /",
            "text/html; charset=shift_jis",
            "記事本文".encode("shift_jis"),
            "shift_jis",
            "記事本文",
        ),
        (404, b"must not read", None, "記事本文".encode(), None, "記事本文"),
    ],
)
async def test_allowed_fetch_returns_response(
    install_client, robots_status, robots_body, content_type, body, charset, text
) -> None:
    """robots許可または404なら記事を取得し、応答の素材を保持する。"""
    requests = []
    robots_stream = BodyStream([robots_body])

    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, stream=robots_stream)
        headers = {"content-type": content_type} if content_type else {}
        return httpx.Response(200, headers=headers, stream=BodyStream([body]))

    install_client(handler)
    result = await article_fetch.fetch_article_response(_URL)
    assert requests == ["https://example.com:8443/robots.txt", str(_URL)]
    assert result == RawResponse(
        url=str(_URL),
        content_type=content_type,
        charset_from_header=charset,
        content=body,
        decoded_text=text,
    )
    if robots_status == 404:
        assert robots_stream.read_count == 0


async def test_robots_disallow_stops_article_fetch(install_client) -> None:
    """明示的なrobots拒否では記事URLへ送信しない。"""
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, content=b"User-agent: *\nDisallow: /news/")

    install_client(handler)
    with pytest.raises(RobotsDisallowedError):
        await article_fetch.fetch_article_response(_URL)
    assert paths == ["/robots.txt"]


@pytest.mark.parametrize(
    ("resource", "status"),
    [(FetchResource.ROBOTS_TXT, 403), (FetchResource.ARTICLE_PAGE, 302)],
)
async def test_response_failure_is_preserved_without_reading_body(
    install_client, resource, status
) -> None:
    """非成功応答は追従や記事取得をせず、応答事実を保持して閉じる。"""
    paths = []
    stream = BodyStream([b"do not consume"])

    def handler(request):
        paths.append(request.url.path)
        if resource == FetchResource.ARTICLE_PAGE and request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            status,
            headers={"Retry-After": " 60 ", "Location": "/elsewhere"},
            stream=stream,
        )

    install_client(handler)
    before = datetime.now(UTC)
    with pytest.raises(HttpResponseError) as caught:
        await article_fetch.fetch_article_response(_URL)
    assert caught.value.status_code == status
    assert caught.value.retry_after == " 60 "
    assert before <= caught.value.received_at <= datetime.now(UTC)
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert paths == (
        ["/robots.txt"]
        if resource == FetchResource.ROBOTS_TXT
        else ["/robots.txt", "/news/article"]
    )
    assert stream.read_count == 0
    assert stream.closed


async def test_robots_timeout_is_transport_failure(install_client) -> None:
    """robots確認の通信タイムアウトを保持し、記事取得へ進まない。"""
    paths = []
    original = httpx.ReadTimeout("no response")

    def handler(request):
        paths.append(request.url.path)
        raise original

    clients = install_client(handler)
    with pytest.raises(HttpTransportError) as caught:
        await article_fetch.fetch_article_response(_URL)
    assert caught.value.failure.reason == HttpTransportFailureReason.TIMEOUT
    assert caught.value.__cause__ is original
    assert paths == ["/robots.txt"]
    assert clients[0].is_closed


async def test_destination_policy_rejection_propagates(monkeypatch) -> None:
    """標準transportの宛先保護を通り、拒否を通信失敗へ変換しない。"""
    original = HostBlockedError("destination denied")

    async def reject(host):
        raise original

    monkeypatch.setattr(external, "resolve_public_host_addresses", reject)
    monkeypatch.setattr(
        external,
        "HttpSettings",
        lambda: SimpleNamespace(egress_proxy_url="http://proxy.internal:3128"),
    )
    with pytest.raises(HostBlockedError) as caught:
        await article_fetch.fetch_article_response(_URL)
    assert caught.value is original


@pytest.mark.parametrize("content_length", [None, "invalid", "1"])
async def test_received_size_limit_cannot_be_bypassed(
    install_client, monkeypatch, content_length
) -> None:
    """Content-Lengthを信用せず、上限を超えた時点で後続受信を止める。"""
    monkeypatch.setattr(article_fetch, "_MAX_RESPONSE_BYTES", 64 * 1024)
    stream = BodyStream([b"x" * (64 * 1024), b"y" * (64 * 1024), b"unread tail"])

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        headers = {"Content-Length": content_length} if content_length else {}
        return httpx.Response(200, headers=headers, stream=stream)

    install_client(handler)
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        await article_fetch.fetch_article_response(_URL)
    assert caught.value.size_basis == ResponseSizeBasis.RECEIVED_DECODED_BODY
    assert caught.value.observed_bytes == 128 * 1024
    assert caught.value.limit_bytes == 64 * 1024
    assert stream.read_count == 2
    assert stream.closed


async def test_body_at_size_limit_is_accepted(install_client, monkeypatch) -> None:
    """本文が上限ちょうどなら取得を完了する。"""
    monkeypatch.setattr(article_fetch, "_MAX_RESPONSE_BYTES", 64 * 1024)
    body = b"x" * (64 * 1024)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, stream=BodyStream([body]))

    install_client(handler)
    assert (await article_fetch.fetch_article_response(_URL)).content == body


async def test_declared_size_limit_stops_before_body(install_client) -> None:
    """robotsのContent-Length超過でも本文を受信せず中断する。"""
    stream = BodyStream([b"unread body"])
    install_client(
        lambda request: httpx.Response(
            200,
            headers={"Content-Length": str(10 * 1024 * 1024 + 1)},
            stream=stream,
        )
    )
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        await article_fetch.fetch_article_response(_URL)
    assert caught.value.resource == FetchResource.ROBOTS_TXT
    assert caught.value.size_basis == ResponseSizeBasis.DECLARED_CONTENT_LENGTH
    assert stream.read_count == 0
    assert stream.closed


async def test_compressed_body_is_limited_after_decompression(
    install_client, monkeypatch
) -> None:
    """圧縮時のサイズが小さくても展開後の本文に上限を適用する。"""
    monkeypatch.setattr(article_fetch, "_MAX_RESPONSE_BYTES", 64 * 1024)
    compressed = gzip.compress(b"x" * (128 * 1024))
    stream = BodyStream([compressed])

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
            stream=stream,
        )

    install_client(handler)
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        await article_fetch.fetch_article_response(_URL)
    assert caught.value.size_basis == ResponseSizeBasis.RECEIVED_DECODED_BODY
    assert stream.closed


async def test_continuous_receive_expires_and_releases_resources(
    install_client, monkeypatch
) -> None:
    """受信が続いていても取得全体の期限で止め、接続を解放する。"""
    monkeypatch.setattr(article_fetch, "_ARTICLE_TIMEOUT_SECONDS", 0.05)
    stream = DripStream()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, stream=stream)

    clients = install_client(handler)
    with pytest.raises(FetchDeadlineExceededError) as caught:
        await article_fetch.fetch_article_response(_URL)
    assert caught.value.resource == FetchResource.ARTICLE_PAGE
    assert caught.value.limit_seconds == 0.05
    assert stream.read_count > 1
    assert stream.closed
    assert clients[0].is_closed


async def test_external_cancellation_propagates_and_releases_resources(
    install_client,
) -> None:
    """呼び出し元のキャンセルを期限超過に変換せず、接続を解放する。"""
    stream = DripStream()
    clients = install_client(lambda request: httpx.Response(200, stream=stream))
    task = asyncio.create_task(article_fetch.fetch_article_response(_URL))
    await stream.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert clients[0].is_closed


async def test_unrelated_timeout_is_not_request_deadline(install_client) -> None:
    """自身の期限に達する前の一般TimeoutErrorは元のまま伝播する。"""
    original = TimeoutError("unrelated timeout")

    def handler(request):
        raise original

    install_client(handler)
    with pytest.raises(TimeoutError) as caught:
        await article_fetch.fetch_article_response(_URL)
    assert caught.value is original
