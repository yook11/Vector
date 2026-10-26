"""送信失敗から再試行・停止を判断し、外部への副作用は持たない。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from math import isfinite

from app.http import retry_prospect as http_retry_prospect
from app.outbox.relay.failures import PublishFailure
from app.outbox.relay.sender import (
    MessageSendError,
    MessageSendTransportError,
    MessageServiceError,
    MessageServiceErrorReason,
)


@dataclass(frozen=True, slots=True)
class RetryDelay:
    """次の送信試行までの待ち時間を、ゼロ以上の時間として保持する。"""

    value: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.value, timedelta):
            raise TypeError("retry_delay must be a timedelta")
        if self.value < timedelta(0):
            raise ValueError("retry_delay must not be negative")


class NonRetryableReason(StrEnum):
    """失敗原因や回数上限により、自動再試行しない理由を表す。"""

    NON_RETRYABLE_FAILURE = "non_retryable_failure"
    RETRY_EXHAUSTED = "retry_exhausted"


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


def is_resolvable_by_retry(error: MessageSendError) -> bool:
    """同じメッセージを送り直すだけで解消しうる送信の失敗かを返す。"""
    match error:
        case MessageSendTransportError():
            return http_retry_prospect.is_resolvable_by_retry(error.failure)
        case MessageServiceError():
            return error.reason in (
                MessageServiceErrorReason.THROTTLED,
                MessageServiceErrorReason.SERVICE_UNAVAILABLE,
            )
        # 設定・資格情報・本文や応答の問題と想定外の失敗は、直すまで変わらない。
        case _:
            return False


def _retry_delay(*, attempt_count: int, jitter: float) -> timedelta:
    """再試行すると決まった試行回数から待ち時間を計算する。"""
    return timedelta(seconds=_RETRY_DELAYS[attempt_count - 1]) * (0.8 + 0.4 * jitter)


def decide_publish_retry(
    error: PublishFailure,
    *,
    attempt_count: int,
    jitter: float,
) -> Retryable | NonRetryable:
    """確保後の試行回数と外部で生成したjitterから配信方針を決める。"""
    if not isinstance(error, PublishFailure):
        raise TypeError("error must be a PublishFailure")
    if type(attempt_count) is not int:
        raise TypeError("attempt_count must be an integer")
    if attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    if type(jitter) not in (int, float):
        raise TypeError("jitter must be a number")
    if not 0 <= jitter <= 1 or not isfinite(jitter):
        raise ValueError("jitter must be finite and between zero and one")

    if not (isinstance(error, MessageSendError) and is_resolvable_by_retry(error)):
        return NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)
    if attempt_count >= MAX_PUBLISH_ATTEMPTS:
        return NonRetryable(NonRetryableReason.RETRY_EXHAUSTED)
    return Retryable(
        RetryDelay(_retry_delay(attempt_count=attempt_count, jitter=jitter))
    )
