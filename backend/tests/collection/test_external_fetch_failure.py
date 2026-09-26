"""外部取得のHTTP失敗が再試行で変わりうるかと、相手が指定した待機期限を保証する。"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.collection.external_fetch_failure import (
    NonRetryableFetchFailure,
    RetryableFetchFailure,
    classify_external_fetch_failure,
)
from app.collection.retry_at import RetryAt
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
)
from app.http.failure import (
    HttpTransportFailureReason as Reason,
)
from app.http.failure import (
    HttpTransportStage as Stage,
)

_RECEIVED = datetime(2026, 9, 14, 12, tzinfo=UTC)
_NOW = _RECEIVED + timedelta(seconds=30)


@pytest.mark.parametrize("status", [302, 400, 401, 403, 404, 410, 451, 499, 501, 505])
def test_http_response_that_current_request_cannot_fetch(status: int) -> None:
    """今の要求では取得できない応答を、再試行しても変わらない失敗と判断する。"""
    exc = HttpResponseError(status_code=status, received_at=_RECEIVED)
    assert classify_external_fetch_failure(exc, now=_NOW) == NonRetryableFetchFailure(
        code="http_response_error"
    )


@pytest.mark.parametrize(
    ("status", "investigate"),
    [
        (407, True),
        (408, False),
        (421, False),
        (425, True),
        (429, False),
        (500, False),
        (502, False),
        (503, False),
        (504, False),
        (507, False),
        (511, True),
        (599, False),
        (100, True),
        (200, True),
        (600, True),
    ],
)
def test_http_response_that_may_change_after_waiting(
    status: int, investigate: bool
) -> None:
    """時間を置けば変わりうる応答を再試行可能とし、環境側の問題は調査対象にする。"""
    exc = HttpResponseError(status_code=status, received_at=_RECEIVED)
    assert classify_external_fetch_failure(exc, now=_NOW) == RetryableFetchFailure(
        code="http_response_error", requires_investigation=investigate
    )


@pytest.mark.parametrize(
    ("failure", "investigate"),
    [
        (HttpTransportFailure(Stage.RECEIVE, Reason.TIMEOUT), False),
        (HttpTransportFailure(Stage.CONNECT, Reason.PROXY, proxy_status=403), False),
        (HttpTransportFailure(Stage.CONNECT, Reason.PROXY, proxy_status=407), True),
        (HttpTransportFailure(Stage.CONNECT, Reason.PROXY, proxy_status=511), True),
        (HttpTransportFailure(Stage.UNKNOWN, Reason.NETWORK_IO), True),
        (HttpTransportFailure(Stage.CONNECT, Reason.UNKNOWN), True),
    ],
)
def test_transport_failure_is_not_read_as_origin_rejection(
    failure: HttpTransportFailure, investigate: bool
) -> None:
    """通信段階の失敗をプロキシの403も含めて取得先の拒否へ読み替えない。"""
    exc = HttpTransportError(failure=failure)
    assert classify_external_fetch_failure(exc, now=_NOW) == RetryableFetchFailure(
        code="http_transport_error", requires_investigation=investigate
    )


def test_host_blocked_by_destination_policy_cannot_fetch() -> None:
    """宛先保護による拒否は再試行しても変わらない失敗と判断する。"""
    assert classify_external_fetch_failure(
        HostBlockedError(), now=_NOW
    ) == NonRetryableFetchFailure(code="host_blocked")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" 120 ", _RECEIVED + timedelta(seconds=120)),
        ("172800", _RECEIVED + timedelta(days=2)),
        ("Mon, 14 Sep 2026 12:02:00 GMT", _RECEIVED + timedelta(minutes=2)),
        ("Monday, 14-Sep-26 12:02:00 GMT", _RECEIVED + timedelta(minutes=2)),
        ("Mon Sep 14 12:02:00 2026", _RECEIVED + timedelta(minutes=2)),
        ("Mon, 14 Sep 2026 21:02:00 +0900", _RECEIVED + timedelta(minutes=2)),
        (None, None),
        ("", None),
        ("0", None),
        ("30", None),
        ("Mon, 14 Sep 2026 11:00:00 GMT", None),
        ("-1", None),
        ("1.5", None),
        ("１２０", None),
        ("not a date", None),
        ("Mon, 14 Sep 2026 99:00:00 GMT", None),
        ("999999999999999999999", None),
    ],
)
def test_retry_after_preserves_valid_future_deadline(
    value: str | None, expected: datetime | None
) -> None:
    """受信時刻基準の待機を短縮せず、経過済み・解釈不能な指示は追加待機にしない。"""
    exc = HttpResponseError(
        status_code=429,
        received_at=_RECEIVED.astimezone(timezone(timedelta(hours=9))),
        retry_after=value,
    )
    assert classify_external_fetch_failure(exc, now=_NOW) == RetryableFetchFailure(
        code="http_response_error",
        retry_at=RetryAt(expected) if expected is not None else None,
    )
    assert exc.retry_after == value


@pytest.mark.parametrize("status", [403, 501])
def test_retry_after_does_not_make_unfetchable_response_retryable(
    status: int,
) -> None:
    """待機ヘッダーがあっても、取得できない応答を再試行可能へ変えない。"""
    exc = HttpResponseError(
        status_code=status, received_at=_RECEIVED, retry_after="120"
    )
    assert classify_external_fetch_failure(exc, now=_NOW) == NonRetryableFetchFailure(
        code="http_response_error"
    )


def test_failure_outside_http_is_left_to_stage() -> None:
    """HTTP起因でない失敗は判断せず、呼び出し側の工程に任せる。"""
    assert (
        classify_external_fetch_failure(ValueError("stage failure"), now=_NOW) is None
    )
