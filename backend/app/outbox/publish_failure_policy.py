"""送信失敗から再試行・停止を判断し、外部への副作用は持たない。"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from math import isfinite

from app.http.failure import HttpTransportFailureKind
from app.outbox.publish_errors import (
    PublishConfigurationError,
    PublishError,
    PublishEventInvalidError,
    PublishPhase,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)


class NonRetryableReason(StrEnum):
    """失敗原因や回数上限により、自動再試行しない理由を表す。"""

    NON_RETRYABLE_FAILURE = "non_retryable_failure"
    RETRY_EXHAUSTED = "retry_exhausted"
    UNCLASSIFIED_FAILURE = "unclassified_failure"
    UNEXPECTED_FAILURE = "unexpected_failure"


@dataclass(frozen=True, slots=True)
class Retryable:
    """失敗原因が再試行対象であり、回数上限はまだ判定していない。"""


@dataclass(frozen=True, slots=True)
class NonRetryable:
    """現在の方針で再試行対象にしない失敗と、その判断理由。"""

    reason: NonRetryableReason


@dataclass(frozen=True, slots=True)
class RetryPublish:
    """次回の配信試行まで待つ時間。"""

    delay: timedelta


@dataclass(frozen=True, slots=True)
class StopPublish:
    """自動配信を停止する判断。"""

    reason: NonRetryableReason


_RETRY_DELAYS = (30, 120, 600, 1800)
_RETRY_TRANSPORT_KINDS = frozenset(
    {
        HttpTransportFailureKind.DNS_RESOLUTION,
        HttpTransportFailureKind.CONNECT,
        HttpTransportFailureKind.CONNECT_TIMEOUT,
        HttpTransportFailureKind.POOL_TIMEOUT,
        HttpTransportFailureKind.WRITE_TIMEOUT,
        HttpTransportFailureKind.READ_TIMEOUT,
        HttpTransportFailureKind.NETWORK_IO,
        HttpTransportFailureKind.REMOTE_PROTOCOL,
        HttpTransportFailureKind.UNKNOWN,
    }
)


def _classify_failure(error: PublishError) -> Retryable | NonRetryable:
    """失敗原因を再試行対象または再試行不可として明示する。"""
    if isinstance(error, PublishTransportError):
        if error.failure.kind in _RETRY_TRANSPORT_KINDS:
            return Retryable()
        if error.failure.kind is HttpTransportFailureKind.TLS:
            return NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)
        if error.failure.kind is HttpTransportFailureKind.PROXY:
            status = error.failure.proxy_status
            if status is None or status == 429 or 500 <= status <= 599:
                return Retryable()
            return NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)
        return NonRetryable(NonRetryableReason.UNEXPECTED_FAILURE)
    if isinstance(error, PublishServiceError):
        if error.reason in (
            PublishServiceReason.THROTTLED,
            PublishServiceReason.SERVICE_UNAVAILABLE,
        ):
            return Retryable()
        if error.reason is PublishServiceReason.UNCLASSIFIED:
            return NonRetryable(NonRetryableReason.UNCLASSIFIED_FAILURE)
        return NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)
    if isinstance(error, PublishConfigurationError | PublishEventInvalidError):
        return NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)
    return NonRetryable(NonRetryableReason.UNEXPECTED_FAILURE)


def decide_publish_failure(
    error: PublishError,
    *,
    attempt_count: int,
    jitter: float,
) -> RetryPublish | StopPublish:
    """確保後の試行回数と外部で生成したjitterから配信方針を決める。"""
    if not isinstance(error, PublishError):
        raise TypeError("error must be a PublishError")
    if (
        isinstance(error, PublishUnexpectedError)
        and error.phase is PublishPhase.CLEANUP
    ):
        raise ValueError("cleanup failures must be handled after publication")
    if type(attempt_count) is not int:
        raise TypeError("attempt_count must be an integer")
    if attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    if type(jitter) not in (int, float):
        raise TypeError("jitter must be a number")
    if not 0 <= jitter <= 1 or not isfinite(jitter):
        raise ValueError("jitter must be finite and between zero and one")

    decision = _classify_failure(error)
    if isinstance(decision, NonRetryable):
        return StopPublish(decision.reason)
    if attempt_count >= 5:
        return StopPublish(NonRetryableReason.RETRY_EXHAUSTED)
    delay = timedelta(seconds=_RETRY_DELAYS[attempt_count - 1]) * (0.8 + 0.4 * jitter)
    return RetryPublish(delay)
