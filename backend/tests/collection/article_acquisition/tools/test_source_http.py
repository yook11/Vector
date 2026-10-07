"""取得先へのGETが、共通の接続設定で本文を上限内で受け取り、失敗を共通HTTPエラーとして伝えることを保証する。"""

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

Handler = Callable[[httpx2.Request], httpx2.Response]


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


def _respond(response: httpx2.Response) -> Handler:
    return lambda _request: response


def _fail(error: Exception) -> Handler:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise error

    return handler


@pytest.fixture
def serve(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Handler], list[httpx2.Request]]:
    """共通GETが作るclientの送信先だけを差し替え、送信したリクエストを記録する。"""

    def install(handler: Handler) -> list[httpx2.Request]:
        requests: list[httpx2.Request] = []

        def record(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            source_http,
            "make_external_async_client",
            lambda **kwargs: httpx2.AsyncClient(
                transport=httpx2.MockTransport(record), **kwargs
            ),
        )
        return requests

    return install


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限の判定だけを見るため、上限を小さくして大きな本文を作らずに済ませる。"""
    monkeypatch.setattr(source_http, "_MAX_RESPONSE_BYTES", _LIMIT)


async def test_successful_response_body_is_returned_with_query_parameters(
    serve,
) -> None:
    """成功応答の本文を返し、呼び出し側の問い合わせ条件を送信に使う。"""
    requests = serve(_respond(httpx2.Response(200, content=b'{"items": []}')))
    response = await get_source_response(_URL, params={"rows": 10})
    assert response.content == b'{"items": []}'
    assert [str(request.url) for request in requests] == [f"{_URL}?rows=10"]


async def test_request_uses_shared_user_agent_and_timeout(serve) -> None:
    """取得元には共通のUser-Agentとtimeoutで送り、Acceptは指定しない。"""
    requests = serve(_respond(httpx2.Response(200)))
    await get_source_response(_URL)
    # 整理前の4か所のreaderが送っていた値と同じ。
    assert requests[0].headers["User-Agent"] == (
        "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
    )
    assert requests[0].headers["Accept"] == "*/*"
    assert requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 30.0,
        "write": 10.0,
        "pool": 5.0,
    }


async def test_request_uses_destination_specific_accept_and_user_agent(
    serve,
) -> None:
    """宛先ごとに渡されたAcceptとUser-Agentで送る。"""
    requests = serve(_respond(httpx2.Response(200)))
    await get_source_response(
        _URL, accept="application/json", user_agent="contact-agent/1.0"
    )
    assert requests[0].headers["Accept"] == "application/json"
    assert requests[0].headers["User-Agent"] == "contact-agent/1.0"


async def test_unsuccessful_response_keeps_status_retry_after_and_receipt(
    serve,
) -> None:
    """非成功応答はstatus・生のRetry-After・受信時刻を解釈せずに伝える。"""
    serve(_respond(httpx2.Response(429, headers={"Retry-After": " 120 "})))
    before = datetime.now(UTC)
    with pytest.raises(HttpResponseError) as caught:
        await get_source_response(_URL)
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
    serve, error: Exception, failure: HttpTransportFailure
) -> None:
    """通信失敗は段階と理由だけを共通の通信エラーに載せ、元の例外を原因に残す。"""
    serve(_fail(error))
    with pytest.raises(HttpTransportError) as caught:
        await get_source_response(_URL)
    assert caught.value.failure == failure
    assert caught.value.__cause__ is error


async def test_failure_while_receiving_body_becomes_common_transport_error(
    serve,
) -> None:
    """本文の受信中に途切れた通信も、受信段階の通信失敗として伝える。"""
    stream = BrokenStream([])
    serve(_respond(httpx2.Response(200, stream=stream)))
    with pytest.raises(HttpTransportError) as caught:
        await get_source_response(_URL)
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
    serve, error: Exception
) -> None:
    """宛先拒否と通信失敗と確認できない例外を、通信障害へ丸めず元のまま伝える。"""
    serve(_fail(error))
    with pytest.raises(type(error)) as caught:
        await get_source_response(_URL)
    assert caught.value is error


async def test_declared_size_over_limit_is_rejected_before_body(serve) -> None:
    """申告された本文の大きさが上限(10MiB)を超えていれば、本文を読まずに中断する。"""
    declared_bytes = 10 * 1024 * 1024 + 1
    stream = BodyStream([b"unread body"])
    serve(
        _respond(
            httpx2.Response(
                200, headers={"Content-Length": str(declared_bytes)}, stream=stream
            )
        )
    )
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        await get_source_response(_URL)
    assert caught.value.size_basis == ResponseSizeBasis.DECLARED_CONTENT_LENGTH
    assert caught.value.observed_bytes == declared_bytes
    assert caught.value.limit_bytes == 10 * 1024 * 1024
    assert stream.read_count == 0
    assert stream.closed


@pytest.mark.parametrize("content_length", [None, "invalid", "1"])
async def test_received_size_over_limit_stops_reading(
    serve, small_limit: None, content_length: str | None
) -> None:
    """Content-Lengthを信用せず、受け取った量が上限を超えた時点で後続を読まない。"""
    stream = BodyStream([b"x" * _LIMIT, b"y" * _LIMIT, b"unread tail"])
    headers = {"Content-Length": content_length} if content_length else {}
    serve(_respond(httpx2.Response(200, headers=headers, stream=stream)))
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        await get_source_response(_URL)
    assert caught.value.size_basis == ResponseSizeBasis.RECEIVED_DECODED_BODY
    assert caught.value.observed_bytes == 2 * _LIMIT
    assert caught.value.limit_bytes == _LIMIT
    assert stream.read_count == 2
    assert stream.closed


async def test_body_at_size_limit_is_accepted(serve, small_limit: None) -> None:
    """本文が上限ちょうどなら取得を完了する。"""
    body = b"x" * _LIMIT
    serve(_respond(httpx2.Response(200, stream=BodyStream([body]))))
    response = await get_source_response(_URL)
    assert response.content == body


async def test_compressed_body_is_limited_after_decompression(
    serve, small_limit: None
) -> None:
    """圧縮時の大きさが小さくても、展開後の本文に上限を適用する。"""
    compressed = gzip.compress(b"x" * (2 * _LIMIT))
    stream = BodyStream([compressed])
    serve(
        _respond(
            httpx2.Response(
                200,
                headers={
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(compressed)),
                },
                stream=stream,
            )
        )
    )
    with pytest.raises(ResponseSizeLimitExceededError) as caught:
        await get_source_response(_URL)
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
    serve, content_type: str, body: bytes
) -> None:
    """文字列は応答が示すcharsetで、示されなければUTF-8で復号する。"""
    serve(
        _respond(
            httpx2.Response(200, headers={"Content-Type": content_type}, content=body)
        )
    )
    response = await get_source_response(_URL)
    assert response.text == "経済産業省"
