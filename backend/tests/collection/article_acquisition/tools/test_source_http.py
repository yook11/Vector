"""取得先へのGETが、非成功応答と通信失敗を共通HTTPエラーとして伝えることを保証する。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from app.collection.article_acquisition.tools.source_http import (
    get_source_response,
)
from app.http.destination_policy import HostBlockedError
from app.http.destination_resolution import HostResolutionError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_URL = "https://example.com/feed"


def _client(*, response: httpx.Response | None = None, error: Exception | None = None):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=response, side_effect=error)
    return client


def _response(
    status_code: int, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        request=httpx.Request("GET", _URL),
    )


async def test_successful_response_is_returned_with_query_parameters() -> None:
    """成功応答はそのまま返し、呼び出し側の問い合わせ条件を送信に使う。"""
    response = _response(200)
    client = _client(response=response)
    assert await get_source_response(client, _URL, params={"rows": 10}) is response
    client.get.assert_awaited_once_with(_URL, params={"rows": 10})


async def test_unsuccessful_response_keeps_status_retry_after_and_receipt() -> None:
    """非成功応答はstatus・生のRetry-After・受信時刻を解釈せずに伝える。"""
    before = datetime.now(UTC)
    with pytest.raises(HttpResponseError) as caught:
        await get_source_response(
            _client(response=_response(429, {"Retry-After": " 120 "})), _URL
        )
    after = datetime.now(UTC)
    assert caught.value.status_code == 429
    assert caught.value.retry_after == " 120 "
    assert caught.value.received_at.tzinfo is UTC
    assert before <= caught.value.received_at <= after
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (
            httpx.ReadTimeout("private-detail"),
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
        await get_source_response(_client(error=error), _URL)
    assert caught.value.failure == failure
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [HostBlockedError(), httpx.UnsupportedProtocol("private-detail")],
)
async def test_destination_block_and_unclassified_failure_propagate_unchanged(
    error: Exception,
) -> None:
    """宛先拒否と通信失敗と確認できない例外を、通信障害へ丸めず元のまま伝える。"""
    with pytest.raises(type(error)) as caught:
        await get_source_response(_client(error=error), _URL)
    assert caught.value is error
