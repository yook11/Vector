"""送信の失敗から再試行・停止を決める判断を、SQSへの送信の失敗などを直接渡し、外部通信やDBを使わず検証する。"""

from datetime import timedelta

import pytest

from app.analysis.assessment.events import (
    AssessedEventInvalidError,
)
from app.analysis.curation.events import (
    CuratedEventInvalidError,
)
from app.aws.sqs.errors import (
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSendCredentialsError,
    MessageSendError,
    MessageSendTransportError,
    MessageSendUnexpectedError,
    SqsServiceError,
    SqsServiceErrorReason,
)
from app.aws.sqs.outgoing_message import (
    MessageBodyMismatchError,
    MessageTooLargeError,
    SqsResponseField,
    SqsResponseInvalidError,
    SqsResponseInvalidReason,
)
from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
)
from app.collection.events import (
    AnalyzableEventInvalidError,
)
from app.db.errors import DatabaseUnexpectedError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.outbox.relay.retry_policy import (
    NonRetryable,
    NonRetryableReason,
    Retryable,
    decide_publish_retry,
)
from app.outbox.relay.route import EventRouteMismatchError
from app.outbox.relay.values import RetryDelay
from app.shared.event_contract import (
    EventInvalid,
    EventInvalidReason,
)


def _throttled():
    return SqsServiceError(
        reason=SqsServiceErrorReason.THROTTLED, service_code="RequestThrottled"
    )


def _transport(reason=HttpTransportFailureReason.TIMEOUT):
    return MessageSendTransportError(
        failure=HttpTransportFailure(HttpTransportStage.CONNECT, reason),
        exception_type="ConnectTimeoutError",
    )


@pytest.mark.parametrize("error", [_throttled(), _transport()])
def test_resolvable_sqs_failure_is_retried_after_first_delay(error):
    """送り直せば直りうるSQSの失敗(スロットリング・通信の待ち切れ)は、1回目の失敗なら30秒後に再試行する。"""
    assert decide_publish_retry(error, attempt_count=1, jitter=0.5) == Retryable(
        RetryDelay(timedelta(seconds=30))
    )


@pytest.mark.parametrize(
    "error",
    [
        SqsServiceError(
            reason=SqsServiceErrorReason.ACCESS_DENIED, service_code="AccessDenied"
        ),
        SqsServiceError(
            reason=SqsServiceErrorReason.UNKNOWN, service_code="FutureError"
        ),
        _transport(HttpTransportFailureReason.CERTIFICATE_VERIFICATION),
        _transport(HttpTransportFailureReason.UNKNOWN),
        MessageSendConfigurationError(
            reason=MessageSendConfigurationReason.MISSING_CREDENTIALS
        ),
        MessageSendCredentialsError(exception_type="CredentialRetrievalError"),
        MessageTooLargeError(),
        SqsResponseInvalidError(
            reason=SqsResponseInvalidReason.MISSING,
            field=SqsResponseField.MESSAGE_ID,
        ),
        MessageBodyMismatchError(),
        AnalyzableEventInvalidError(
            EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
        ),
        IncompleteArticleEventInvalidError(
            EventInvalid(
                reason=EventInvalidReason.INVALID_PAYLOAD,
                issues=(),
            )
        ),
        AssessedEventInvalidError(
            EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
        ),
        CuratedEventInvalidError(
            EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
        ),
        EventRouteMismatchError(),
        MessageSendUnexpectedError(exception_type="ParamValidationError"),
    ],
)
def test_non_resolvable_failure_stops_as_non_retryable(error):
    """送り直しても直らない失敗は、未分類のSQSの拒否・通信の失敗や想定外も含め、再試行しない失敗として止める。"""
    assert decide_publish_retry(error, attempt_count=1, jitter=0.5) == NonRetryable(
        NonRetryableReason.NON_RETRYABLE_FAILURE
    )


def test_unrecognized_send_failure_is_not_retried():
    """今後追加される未対応の送信の失敗を、暗黙に再試行せず再試行しない失敗として止める。"""

    class FutureSendError(MessageSendError):
        pass

    assert decide_publish_retry(
        FutureSendError("future"), attempt_count=1, jitter=0
    ) == NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)


@pytest.mark.parametrize(
    "error",
    [
        MessageSendConfigurationError(
            reason=MessageSendConfigurationReason.MISSING_CREDENTIALS
        ),
        SqsServiceError(
            reason=SqsServiceErrorReason.UNKNOWN, service_code="FutureError"
        ),
        MessageSendUnexpectedError(exception_type="ParamValidationError"),
    ],
)
def test_non_resolvable_failure_at_retry_limit_is_not_exhaustion(error):
    """上限の5回目でも、送り直しても直らない失敗は上限到達ではなく再試行しない失敗として止める。"""
    assert decide_publish_retry(error, attempt_count=5, jitter=0.5) == NonRetryable(
        NonRetryableReason.NON_RETRYABLE_FAILURE
    )


@pytest.mark.parametrize("attempt", [5, 6, 100])
def test_resolvable_failure_stops_at_fifth_attempt(attempt):
    """初回を含めた5回目の失敗で、送り直せる失敗でも追加の再試行を止める。"""
    assert decide_publish_retry(
        _transport(), attempt_count=attempt, jitter=0
    ) == NonRetryable(NonRetryableReason.RETRY_EXHAUSTED)


@pytest.mark.parametrize(
    ("attempt", "seconds"), [(1, 30), (2, 120), (3, 600), (4, 1800)]
)
def test_delay_schedule(attempt, seconds):
    """jitterが中央のとき、1〜4回目の失敗の待ち時間は30秒・2分・10分・30分になる。"""
    assert decide_publish_retry(
        _transport(), attempt_count=attempt, jitter=0.5
    ) == Retryable(RetryDelay(timedelta(seconds=seconds)))


@pytest.mark.parametrize(("jitter", "seconds"), [(0, 24), (1, 36)])
def test_jitter_endpoints(jitter, seconds):
    """jitterの両端では、1回目の待ち時間が基準の30秒から±20%の24秒と36秒になる。"""
    assert decide_publish_retry(
        _transport(), attempt_count=1, jitter=jitter
    ) == Retryable(RetryDelay(timedelta(seconds=seconds)))


@pytest.mark.parametrize(
    "error",
    [ValueError("db failed"), DatabaseUnexpectedError(), KeyboardInterrupt(), None],
)
def test_non_publish_failure_is_rejected(error):
    """DBやプロセスの失敗を配信失敗の判断対象にしない。"""
    with pytest.raises(TypeError):
        decide_publish_retry(error, attempt_count=1, jitter=0)


@pytest.mark.parametrize("attempt", [True, False, 1.0, "1", None])
def test_non_integer_attempt_count_is_rejected(attempt):
    """boolを含む整数以外の回数を拒否する。"""
    with pytest.raises(TypeError):
        decide_publish_retry(_transport(), attempt_count=attempt, jitter=0)


@pytest.mark.parametrize("attempt", [0, -1])
def test_non_positive_attempt_count_is_rejected(attempt):
    """確保前の回数を誤って渡すことを防ぐ。"""
    with pytest.raises(ValueError):
        decide_publish_retry(_transport(), attempt_count=attempt, jitter=0)


@pytest.mark.parametrize(
    "jitter", [-0.01, 1.01, float("nan"), float("inf"), -float("inf")]
)
def test_out_of_range_jitter_is_rejected(jitter):
    """非有限値や範囲外の値から待ち時間を生成しない。"""
    with pytest.raises(ValueError):
        decide_publish_retry(_transport(), attempt_count=1, jitter=jitter)


@pytest.mark.parametrize("jitter", [True, False, "0.5", None])
def test_non_numeric_jitter_is_rejected(jitter):
    """boolや文字列を乱数値とみなさない。"""
    with pytest.raises(TypeError):
        decide_publish_retry(_transport(), attempt_count=1, jitter=jitter)


def test_retryable_rejects_raw_timedelta():
    """再試行の判断は、検証済みのRetryDelayでない生のtimedeltaを待ち時間として受け取らない。"""
    with pytest.raises(TypeError):
        Retryable(timedelta(seconds=30))


@pytest.mark.parametrize("reason", ["non_retryable_failure", "", "custom_reason", None])
def test_non_retryable_requires_reason_enum(reason):
    """値が一致する文字列でもEnumへの暗黙変換は行わない。"""
    with pytest.raises(TypeError):
        NonRetryable(reason)
