"""外部取得のHTTP失敗が、時間を置けば結果が変わりうるかと相手の待機指示を判断する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from app.collection.retry_at import RetryAt
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import HttpTransportFailureReason, HttpTransportStage


@dataclass(frozen=True, slots=True)
class RetryableFetchFailure:
    """時間を置けば結果が変わりうる失敗と、相手が指定した待機時刻を表す。"""

    code: str
    retry_at: RetryAt | None = None
    """再試行可能なUTC日時で、指定なし・無効・0秒・期限経過済みならNone。"""
    requires_investigation: bool = False
    """取得先の拒否ではなく、環境側の問題か分類が確かでない。"""


@dataclass(frozen=True, slots=True)
class NonRetryableFetchFailure:
    """今の要求では取得できず、再試行しても結果が変わらない失敗を表す。"""

    code: str


def _retry_at(exc: HttpResponseError, *, now: datetime) -> RetryAt | None:
    """有効な未来の待機時刻をUTCで返し、指定なし・無効・0秒・期限経過済みならNoneを返す。"""
    value = exc.retry_after
    if value is None or not (value := value.strip()):
        return None
    try:
        if value.isascii() and value.isdecimal():
            seconds = int(value)
            if seconds == 0:
                return None
            candidate = exc.received_at.astimezone(UTC) + timedelta(seconds=seconds)
        else:
            candidate = parsedate_to_datetime(value)
            # HTTPの旧asctime形式にはタイムゾーン指定がない。
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=UTC)
            candidate = candidate.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None
    return RetryAt(candidate) if candidate > now else None


def classify_external_fetch_failure(
    exc: Exception, *, now: datetime
) -> RetryableFetchFailure | NonRetryableFetchFailure | None:
    """HTTP起因の失敗の見込みだけを返し、後始末とそれ以外の失敗は工程に任せる。"""
    if isinstance(exc, HttpResponseError):
        status = exc.status_code
        investigate = status in (407, 425, 511) or not 300 <= status < 600
        retry = (
            investigate
            or status in (408, 421, 429)
            or (500 <= status < 600 and status not in (501, 505))
        )
        if retry:
            return RetryableFetchFailure(
                code=exc.CODE,
                retry_at=_retry_at(exc, now=now),
                requires_investigation=investigate,
            )
        return NonRetryableFetchFailure(code=exc.CODE)

    if isinstance(exc, HttpTransportError):
        failure = exc.failure
        return RetryableFetchFailure(
            code=exc.CODE,
            requires_investigation=(
                failure.stage is HttpTransportStage.UNKNOWN
                or failure.reason is HttpTransportFailureReason.UNKNOWN
                or failure.proxy_status in (407, 511)
            ),
        )

    if isinstance(exc, HostBlockedError):
        return NonRetryableFetchFailure(code="host_blocked")

    return None
