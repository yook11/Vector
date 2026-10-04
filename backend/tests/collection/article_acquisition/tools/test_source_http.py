"""取得先へのGETが、本文を上限内で受け取り、失敗を共通HTTPエラーとして伝えることを保証する。"""

import gzip
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime

import httpx2
import pytest

from app.collection.article_acquisition.errors import ResponseSizeLimitExceededError
from app.collection.article_acquisition.tools import source_http
from app.collection.article_acquisition.tools.source_http import (
    get_source_response,
)
from app.collection.response_size import ResponseSizeBasis
from app.http.destination_policy import HostBlockedError
from app.http.destination_resolution import HostResolutionError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_URL = "https://example.com/feed"
_LIMIT = 64 * 1024


class BodyStream(httpx2.AsyncByteStream):
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


class BrokenStream(BodyStream):
    """最初のチャンクの後で受信が途切れる応答本文。"""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read_count += 1
        yield b"partial"
        raise httpx2.ReadError("private-detail")


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _respond(response: httpx2.Response) -> httpx2.AsyncClient:
    return _client(lambda _request: response)


def _fail(error: Exception) -> httpx2.AsyncClient:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise error

    return _client(handler)


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限の判定だけを見るため、上限を小さくして大きな本文を作らずに済ませる。"""
    monkeypatch.setattr(source_http, "_MAX_RESPONSE_BYTES", _LIMIT)


async def test_successful_response_body_is_returned_with_query_parameters() -> None:
    """成功応答の本文を返し、呼び出し側の問い合わせ条件を送信に使う。"""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b'{"items": []}')

    async with _client(handler) as client:
        response = await get_source_response(client, _URL, params={"rows": 10})
    assert response.content == b'{"items": []}'
    assert [str(request.url) for request in requests] == [f"{_URL}?rows=10"]


async def test_unsuccessful_response_keeps_status_retry_after_and_receipt() -> None:
    """非成功応答はstatus・生のRetry-After・受信時刻を解釈せずに伝える。"""
    before = datetime.now(UTC)
    with pytest.raises(HttpResponseError) as caught:
        async with _respond(
            httpx2.Response(429, headers={"Retry-After": " 120 "})
        ) as client:
            await get_source_response(client, _URL)
    after = datetime.now(UTC)
    assert caught.value.status_code == 429
    assert caught.value.retry_after == " 120 "
    assert caught.value.received_at.tzinfo is UTC
    assert before <= caught.value.received_at <= after
    assert isinstance(caught.value.__cause__, httpx2.HTTPStatusError)


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (
            httpx2.ReadTimeout("private-detail"),
            HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            ),
        ),
        (
            HostResolutionError("private-detail"),
            HttpTransportFailure(
                HttpTransportStage.PREPARATION,
                HttpTransportFailureReason.DNS_RESOLUTION,
            ),
        ),
    ],
)
async def test_transport_failure_becomes_common_transport_error(
    error: Exception, failure: HttpTransportFailure
) -> None:
    """通信失敗は段階と理由だけを共通の通信エラーに載せ、元の例外を原因に残す。"""
    with pytest.raises(HttpTransportError) as caught:
        async with _fail(error) as client:
            await get_source_response(client, _URL)
    assert caught.value.failure == failure
    assert caught.value.__cause__ is error


async def test_failure_while_receiving_body_becomes_common_transport_error() -> None:
    """本文の受信中に途切れた通信も、受信段階の通信失敗として伝える。"""
    stream = BrokenStream([])
    with pytest.raises(HttpTransportError) as caught:
        async with _respond(httpx2.Response(200, stream=stream)) as client:
            await get_source_response(client, _URL)
    assert caught.value.failure == HttpTransportFailure(
        HttpTransportStage.RECEIVE, HttpTransportFailureReason.NETWORK_IO
    )
    assert isinstance(caught.value.__cause__, httpx2.ReadError)
    assert stream.closed


@pytest.mark.parametrize(
    "error",
    [HostBlockedError(), httpx2.UnsupportedProtocol("private-detail")],
)
async def test_destination_block_and_unclassified_failure_propagate_unchanged(
    error: Exception,
) -> None:
    """宛先拒否と通信失敗と確認できない例外を、通信障害へ丸めず元のまま伝える。"""
    with pytest.raises(type(error)) as caught:
        async with _fail(error) as client:
            await get_source_response(client, _URL)
    assert caught.value is error


async def test_declared_size_over_limit_is_rejected_before_body() -> None:
    """申告された本文の大きさが上限(10MiB)を超えていれば、本文を読まずに中断する。"""
    declared_bytes = 10 * 1024 * 1024 + 1
    stream = BodyStream([b"unread body"])
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        async with _respond(
            httpx2.Response(
                200, headers={"Content-Length": str(declared_bytes)}, stream=stream
            )
        ) as client:
            await get_source_response(client, _URL)
    assert caught.value.size_basis == ResponseSizeBasis.DECLARED_CONTENT_LENGTH
    assert caught.value.observed_bytes == declared_bytes
    assert caught.value.limit_bytes == 10 * 1024 * 1024
    assert stream.read_count == 0
    assert stream.closed


@pytest.mark.parametrize("content_length", [None, "invalid", "1"])
async def test_received_size_over_limit_stops_reading(
    small_limit: None, content_length: str | None
) -> None:
    """Content-Lengthを信用せず、受け取った量が上限を超えた時点で後続を読まない。"""
    stream = BodyStream([b"x" * _LIMIT, b"y" * _LIMIT, b"unread tail"])
    headers = {"Content-Length": content_length} if content_length else {}
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        async with _respond(
            httpx2.Response(200, headers=headers, stream=stream)
        ) as client:
            await get_source_response(client, _URL)
    assert caught.value.size_basis == ResponseSizeBasis.RECEIVED_DECODED_BODY
    assert caught.value.observed_bytes == 2 * _LIMIT
    assert caught.value.limit_bytes == _LIMIT
    assert stream.read_count == 2
    assert stream.closed


async def test_body_at_size_limit_is_accepted(small_limit: None) -> None:
    """本文が上限ちょうどなら取得を完了する。"""
    body = b"x" * _LIMIT
    async with _respond(httpx2.Response(200, stream=BodyStream([body]))) as client:
        response = await get_source_response(client, _URL)
    assert response.content == body


async def test_compressed_body_is_limited_after_decompression(
    small_limit: None,
) -> None:
    """圧縮時の大きさが小さくても、展開後の本文に上限を適用する。"""
    compressed = gzip.compress(b"x" * (2 * _LIMIT))
    stream = BodyStream([compressed])
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        async with _respond(
            httpx2.Response(
                200,
                headers={
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(compressed)),
                },
                stream=stream,
            )
        ) as client:
            await get_source_response(client, _URL)
    assert caught.value.size_basis == ResponseSizeBasis.RECEIVED_DECODED_BODY
    assert stream.closed


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/rss+xml; charset=shift_jis", "経済産業省".encode("shift_jis")),
        ("application/rss+xml", "経済産業省".encode()),
    ],
)
async def test_text_is_decoded_with_declared_charset_or_utf8(
    content_type: str, body: bytes
) -> None:
    """文字列は応答が示すcharsetで、示されなければUTF-8で復号する。"""
    async with _respond(
        httpx2.Response(200, headers={"Content-Type": content_type}, content=body)
    ) as client:
        response = await get_source_response(client, _URL)
    assert response.text == "経済産業省"
