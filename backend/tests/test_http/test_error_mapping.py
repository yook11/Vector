"""HTTPの失敗変換で発生事実を保持し、対象外を誤分類しないことを検証する。"""

from datetime import UTC, datetime

import httpx
import pytest

from app.http.error_mapping import (
    http_response_error_from_exception,
    http_transport_error_from_exception,
)
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            httpx.ConnectError("connection failed"),
            HttpTransportFailure(
                HttpTransportStage.CONNECT, HttpTransportFailureReason.NETWORK_IO
            ),
        ),
        (
            httpx.ReadTimeout("response timed out"),
            HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            ),
        ),
        (
            HostResolutionError("DNS failed"),
            HttpTransportFailure(
                HttpTransportStage.PREPARATION,
                HttpTransportFailureReason.DNS_RESOLUTION,
            ),
        ),
        (
            httpx.ProxyError("403 Forbidden"),
            HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.PROXY,
                proxy_status=403,
            ),
        ),
    ],
)
def test_transport_conversion_preserves_failure_facts(
    exc: Exception, expected: HttpTransportFailure
) -> None:
    """通信失敗の段階・理由・proxy情報を失わず伝える。"""
    result = http_transport_error_from_exception(exc)

    assert result is not None
    assert result.failure == expected


def test_unspecified_transport_failure_remains_unknown() -> None:
    """通信失敗と分かるが詳細不明な例外では段階も理由も断定しない。"""
    result = http_transport_error_from_exception(httpx.TransportError("unspecified"))

    assert result is not None
    assert result.failure == HttpTransportFailure(
        HttpTransportStage.UNKNOWN, HttpTransportFailureReason.UNKNOWN
    )


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("unexpected value"),
        TimeoutError("unspecified operation"),
        HostBlockedError("destination denied"),
        httpx.LocalProtocolError("invalid request"),
    ],
)
def test_non_transport_errors_remain_unconverted(exc: Exception) -> None:
    """通信失敗と確認できない例外をHTTPのunknownへ丸めない。"""
    assert http_transport_error_from_exception(exc) is None


def test_origin_refusal_is_not_a_transport_failure() -> None:
    """取得先から届いた403応答をproxy接続拒否へ誤変換しない。"""
    response = httpx.Response(
        403, request=httpx.Request("GET", "https://example.invalid/article")
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()

    assert http_transport_error_from_exception(caught.value) is None


@pytest.mark.parametrize("status_code", [302, 403, 429, 503])
def test_response_conversion_preserves_origin_status(status_code: int) -> None:
    """取得先のステータスを別の原因へ置き換えず伝える。"""
    response = httpx.Response(
        status_code, request=httpx.Request("GET", "https://example.invalid/article")
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()

    result = http_response_error_from_exception(
        caught.value, received_at=datetime(2026, 9, 13, 12, 0, 2, tzinfo=UTC)
    )

    assert result.status_code == status_code


@pytest.mark.parametrize(
    "retry_after",
    [None, "", "60", " 60 ", "Sun, 13 Sep 2026 12:01:02 GMT", "later", "-1"],
)
def test_response_conversion_preserves_retry_after_without_interpretation(
    retry_after: str | None,
) -> None:
    """待機指示の欠如・空値・日時・不正値を勝手に解釈せず伝える。"""
    headers = {} if retry_after is None else {"rEtRy-AfTeR": retry_after}
    response = httpx.Response(
        503,
        headers=headers,
        request=httpx.Request("GET", "https://example.invalid/article"),
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()

    result = http_response_error_from_exception(
        caught.value, received_at=datetime(2026, 9, 13, 12, 0, 2, tzinfo=UTC)
    )

    assert result.retry_after == retry_after


def test_response_conversion_preserves_recorded_receive_time() -> None:
    """受信時刻を変換時の現在時刻や再試行予定時刻で置き換えない。"""
    received_at = datetime(2026, 9, 13, 12, 0, 2, tzinfo=UTC)
    response = httpx.Response(
        429,
        headers={"Retry-After": "60"},
        request=httpx.Request("GET", "https://example.invalid/article"),
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()

    result = http_response_error_from_exception(caught.value, received_at=received_at)

    assert result.received_at == received_at
