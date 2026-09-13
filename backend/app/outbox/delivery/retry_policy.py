"""送信失敗から再試行・停止を判断し、外部への副作用は持たない。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from math import isfinite

from app.http.failure import HttpTransportFailureReason
from app.outbox.delivery.values import RetryDelay
from app.outbox.publishing.errors import (
    PublishConfigurationError,
    PublishError,
    PublishEventInvalidError,
    PublishIntegrityError,
    PublishResponseInvalidError,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
)


class NonRetryableReason(StrEnum):
    """失敗原因や回数上限により、自動再試行しない理由を表す。"""

    NON_RETRYABLE_FAILURE = "non_retryable_failure"
    RETRY_EXHAUSTED = "retry_exhausted"
    UNCLASSIFIED_FAILURE = "unclassified_failure"
    UNEXPECTED_FAILURE = "unexpected_failure"


@dataclass(frozen=True, slots=True)
class Retryable:
    """原因と回数上限を確認し、指定の待ち時間で再試行する判断。"""

    delay: RetryDelay

    def __post_init__(self) -> None:
        if not isinstance(self.delay, RetryDelay):
            raise TypeError("delay must be a RetryDelay")


@dataclass(frozen=True, slots=True)
class NonRetryable:
    """失敗原因または回数上限により自動再試行しない判断。"""

    reason: NonRetryableReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, NonRetryableReason):
            raise TypeError("reason must be a NonRetryableReason")


MAX_PUBLISH_ATTEMPTS = 5

_RETRY_DELAYS = (30, 120, 600, 1800)
_RETRY_TRANSPORT_REASONS = frozenset(
    {
        HttpTransportFailureReason.TIMEOUT,
        HttpTransportFailureReason.DNS_RESOLUTION,
        HttpTransportFailureReason.NETWORK_IO,
        HttpTransportFailureReason.PROTOCOL_VIOLATION,
        HttpTransportFailureReason.UNKNOWN,
    }
)
_RETRY_SERVICE_REASONS = frozenset(
    {
        PublishServiceReason.THROTTLED,
        PublishServiceReason.SERVICE_UNAVAILABLE,
    }
)


def _is_retry_candidate(error: PublishError) -> bool:
    """回数を見ず、失敗の性質として再試行対象かを返す。"""
    if isinstance(error, PublishTransportError):
        if error.failure.reason in _RETRY_TRANSPORT_REASONS:
            return True
        if error.failure.reason is HttpTransportFailureReason.PROXY:
            return _is_retryable_proxy_status(error.failure.proxy_status)
        return False
    if isinstance(error, PublishServiceError):
        return error.reason in _RETRY_SERVICE_REASONS
    return False


def _non_retryable_reason(error: PublishError) -> NonRetryableReason:
    """再試行対象でない失敗の性質から、停止理由を決める。"""
    if isinstance(error, PublishTransportError):
        if error.failure.reason is HttpTransportFailureReason.TLS:
            return NonRetryableReason.NON_RETRYABLE_FAILURE
        if error.failure.reason is HttpTransportFailureReason.PROXY:
            return NonRetryableReason.NON_RETRYABLE_FAILURE
        return NonRetryableReason.UNEXPECTED_FAILURE
    if isinstance(error, PublishServiceError):
        if error.reason is PublishServiceReason.UNCLASSIFIED:
            return NonRetryableReason.UNCLASSIFIED_FAILURE
        return NonRetryableReason.NON_RETRYABLE_FAILURE
    if isinstance(
        error,
        (
            PublishConfigurationError
            | PublishEventInvalidError
            | PublishIntegrityError
            | PublishResponseInvalidError
        ),
    ):
        return NonRetryableReason.NON_RETRYABLE_FAILURE
    return NonRetryableReason.UNEXPECTED_FAILURE


def _is_retryable_proxy_status(status: int | None) -> bool:
    return status is None or status == 429 or 500 <= status <= 599


def _retry_delay(*, attempt_count: int, jitter: float) -> timedelta:
    """再試行すると決まった試行回数から待ち時間を計算する。"""
    return timedelta(seconds=_RETRY_DELAYS[attempt_count - 1]) * (0.8 + 0.4 * jitter)


def decide_publish_retry(
    error: PublishError,
    *,
    attempt_count: int,
    jitter: float,
) -> Retryable | NonRetryable:
    """確保後の試行回数と外部で生成したjitterから配信方針を決める。"""
    if not isinstance(error, PublishError):
        raise TypeError("error must be a PublishError")
    if type(attempt_count) is not int:
        raise TypeError("attempt_count must be an integer")
    if attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    if type(jitter) not in (int, float):
        raise TypeError("jitter must be a number")
    if not 0 <= jitter <= 1 or not isfinite(jitter):
        raise ValueError("jitter must be finite and between zero and one")

    if not _is_retry_candidate(error):
        return NonRetryable(_non_retryable_reason(error))
    if attempt_count >= MAX_PUBLISH_ATTEMPTS:
        return NonRetryable(NonRetryableReason.RETRY_EXHAUSTED)
    return Retryable(
        RetryDelay(_retry_delay(attempt_count=attempt_count, jitter=jitter))
    )
