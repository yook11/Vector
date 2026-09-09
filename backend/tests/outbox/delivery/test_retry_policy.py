"""再試行・停止の契約を、外部通信やDBを使わず検証する。"""

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from app.http.failure import HttpTransportFailure, HttpTransportFailureKind
from app.outbox.delivery.retry_policy import (
    NonRetryable,
    NonRetryableReason,
    Retryable,
    decide_publish_retry,
)
from app.outbox.delivery.values import RetryDelay
from app.outbox.publishing.errors import (
    PublishCleanupError,
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishEventInvalidError,
    PublishEventInvalidReason,
    PublishPhase,
    PublishResponseInvalidError,
    PublishResponseInvalidReason,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)


def transport(kind=HttpTransportFailureKind.CONNECT, reached=False, status=None):
    return PublishTransportError(
        failure=HttpTransportFailure(kind, reached, proxy_status=status)
    )


@pytest.mark.parametrize("kind", list(HttpTransportFailureKind))
@pytest.mark.parametrize("reached", [False, True])
def test_all_transport_kinds_and_delivery_uncertainty(kind, reached):
    """到達可能性は重複許容の配信方針を変えない。"""
    result = decide_publish_retry(transport(kind, reached), attempt_count=1, jitter=0.5)
    if kind is HttpTransportFailureKind.TLS:
        assert result == NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)
    else:
        assert result == Retryable(RetryDelay(timedelta(seconds=30)))


@pytest.mark.parametrize("reason", list(PublishServiceReason))
@pytest.mark.parametrize("status", [None, 403, 503])
def test_all_service_reasons_ignore_raw_status(reason, status):
    """AWS側の応答を再分類せず、共通reasonだけで決める。"""
    error = PublishServiceError(
        reason=reason,
        service_error_code="private-code",
        status_code=status,
        request_id="private-id",
    )
    result = decide_publish_retry(error, attempt_count=1, jitter=0.5)
    if reason in (
        PublishServiceReason.THROTTLED,
        PublishServiceReason.SERVICE_UNAVAILABLE,
    ):
        assert result == Retryable(RetryDelay(timedelta(seconds=30)))
    elif reason is PublishServiceReason.UNCLASSIFIED:
        assert result == NonRetryable(NonRetryableReason.UNCLASSIFIED_FAILURE)
    else:
        assert result == NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)


@pytest.mark.parametrize("reason", list(PublishConfigurationReason))
def test_all_configuration_reasons_stop(reason):
    """取得失敗を含む資格情報・設定の問題を自動再試行しない。"""
    assert decide_publish_retry(
        PublishConfigurationError(reason=reason), attempt_count=1, jitter=0
    ) == NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)


@pytest.mark.parametrize("reason", list(PublishEventInvalidReason))
def test_all_event_invalid_reasons_stop(reason):
    """不正なイベントは送信回数にかかわらず停止する。"""
    assert decide_publish_retry(
        PublishEventInvalidError(reason=reason), attempt_count=1, jitter=0
    ) == NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)


@pytest.mark.parametrize("phase", list(PublishPhase))
def test_unexpected_phases_stop_delivery(phase):
    """送信処理の想定外は発生段階にかかわらず停止する。"""
    error = PublishUnexpectedError(
        original_exception=RuntimeError("private"), phase=phase
    )
    assert decide_publish_retry(error, attempt_count=1, jitter=0) == NonRetryable(
        NonRetryableReason.UNEXPECTED_FAILURE
    )


@pytest.mark.parametrize(
    ("status", "retry"),
    [
        (None, True),
        (429, True),
        (499, False),
        (500, True),
        (599, True),
        (600, False),
        (200, False),
        (400, False),
        (403, False),
        (407, False),
    ],
)
def test_proxy_status_boundaries(status, retry):
    """proxy拒否と一時的なproxy障害を区別する。"""
    result = decide_publish_retry(
        transport(HttpTransportFailureKind.PROXY, status=status),
        attempt_count=1,
        jitter=0.5,
    )
    assert result == (
        Retryable(RetryDelay(timedelta(seconds=30)))
        if retry
        else NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)
    )


@pytest.mark.parametrize(("attempt", "base"), [(1, 30), (2, 120), (3, 600), (4, 1800)])
@pytest.mark.parametrize(("jitter", "percent"), [(0, 80), (0.5, 100), (1, 120)])
def test_delay_schedule_and_jitter_endpoints(attempt, base, jitter, percent):
    """各試行の待ち時間と±20%の境界を固定する。"""
    assert decide_publish_retry(
        transport(),
        attempt_count=attempt,
        jitter=jitter,
    ) == Retryable(RetryDelay(timedelta(seconds=base * percent / 100)))


@pytest.mark.parametrize("attempt", [5, 6, 100])
def test_retryable_failure_stops_at_fifth_attempt(attempt):
    """初回を含めた5回目の失敗で追加の再試行を止める。"""
    assert decide_publish_retry(transport(), attempt_count=attempt, jitter=0) == (
        NonRetryable(NonRetryableReason.RETRY_EXHAUSTED)
    )


@pytest.mark.parametrize(
    "error",
    [
        transport(HttpTransportFailureKind.TLS),
        PublishConfigurationError(
            reason=PublishConfigurationReason.MISSING_CREDENTIALS
        ),
        PublishEventInvalidError(reason=PublishEventInvalidReason.INVALID_PAYLOAD),
        PublishServiceError(
            reason=PublishServiceReason.UNCLASSIFIED,
            service_error_code="x",
            status_code=500,
        ),
        PublishUnexpectedError(
            original_exception=ValueError(), phase=PublishPhase.SEND
        ),
        PublishError(),
    ],
)
@pytest.mark.parametrize("attempt", [5, 6])
def test_immediate_stop_reason_takes_precedence_over_retry_limit(error, attempt):
    """即停止理由を上限到達で上書きしない。"""
    assert decide_publish_retry(error, attempt_count=attempt, jitter=0) == (
        decide_publish_retry(error, attempt_count=1, jitter=0)
    )


def test_unrecognized_publish_error_stops_as_unexpected():
    """今後追加される未対応の種類を暗黙に再試行しない。"""

    class FuturePublishError(PublishError):
        pass

    for error in (PublishError(), FuturePublishError()):
        assert decide_publish_retry(error, attempt_count=1, jitter=0) == (
            NonRetryable(NonRetryableReason.UNEXPECTED_FAILURE)
        )


@pytest.mark.parametrize(
    "error",
    [
        ValueError("db failed"),
        KeyboardInterrupt(),
        None,
        PublishCleanupError(original_exception=RuntimeError()),
    ],
)
def test_non_publish_error_is_rejected(error):
    """DBやプロセスの失敗を配信失敗の判断対象にしない。"""
    with pytest.raises(TypeError):
        decide_publish_retry(error, attempt_count=1, jitter=0)


@pytest.mark.parametrize("attempt", [True, False, 1.0, "1", None])
def test_non_integer_attempt_count_is_rejected(attempt):
    """boolを含む整数以外の回数を拒否する。"""
    with pytest.raises(TypeError):
        decide_publish_retry(transport(), attempt_count=attempt, jitter=0)


@pytest.mark.parametrize("attempt", [0, -1])
def test_non_positive_attempt_count_is_rejected(attempt):
    """確保前の回数を誤って渡すことを防ぐ。"""
    with pytest.raises(ValueError):
        decide_publish_retry(transport(), attempt_count=attempt, jitter=0)


@pytest.mark.parametrize(
    "jitter", [-0.01, 1.01, float("nan"), float("inf"), -float("inf")]
)
def test_out_of_range_jitter_is_rejected(jitter):
    """非有限値や範囲外の値から待ち時間を生成しない。"""
    with pytest.raises(ValueError):
        decide_publish_retry(transport(), attempt_count=1, jitter=jitter)


@pytest.mark.parametrize("jitter", [True, False, "0.5", None])
def test_non_numeric_jitter_is_rejected(jitter):
    """boolや文字列を乱数値とみなさない。"""
    with pytest.raises(TypeError):
        decide_publish_retry(transport(), attempt_count=1, jitter=jitter)


def test_decision_is_repeatable_and_does_not_mutate_error():
    """診断情報と元例外のチェーンを変更せず同じ結果を返す。"""
    error = PublishServiceError(
        reason=PublishServiceReason.THROTTLED,
        service_error_code="AccessDenied",
        status_code=403,
        request_id="private",
    )
    error.__cause__ = RuntimeError("private-message")
    before = vars(error).copy()
    cause = error.__cause__
    expected = Retryable(RetryDelay(timedelta(seconds=120)))
    assert decide_publish_retry(error, attempt_count=2, jitter=0.5) == expected
    assert vars(error) == before
    assert error.__cause__ is cause
    error.service_error_code = "different"
    error.request_id = "different"
    assert decide_publish_retry(error, attempt_count=2, jitter=0.5) == expected
    error.service_error_code = before["service_error_code"]
    error.request_id = before["request_id"]
    assert vars(error) == before
    assert error.__cause__ is cause


@pytest.mark.parametrize(
    "result",
    [
        Retryable(RetryDelay(timedelta(seconds=30))),
        NonRetryable(NonRetryableReason.RETRY_EXHAUSTED),
    ],
)
def test_decisions_are_immutable(result):
    """handlerへ渡した判断結果を後から変更できない。"""
    with pytest.raises(FrozenInstanceError):
        if isinstance(result, Retryable):
            result.delay = timedelta(0)
        else:
            result.reason = NonRetryableReason.UNEXPECTED_FAILURE


@pytest.mark.parametrize("attempt", [1, 4, 5, 6, 100])
def test_integrity_failure_stops_without_reinterpreting_request_id(attempt):
    """本文不一致は試行回数にかかわらず停止し、診断情報を判断へ混入させない。"""
    from app.outbox.publishing.errors import (
        PublishIntegrityError,
        PublishIntegrityReason,
    )

    error = PublishIntegrityError(
        reason=PublishIntegrityReason.BODY_CHECKSUM_MISMATCH, request_id="private"
    )
    before = vars(error).copy()
    assert decide_publish_retry(
        error, attempt_count=attempt, jitter=0.5
    ) == NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)
    assert vars(error) == before


@pytest.mark.parametrize("reason", list(PublishResponseInvalidReason))
@pytest.mark.parametrize("attempt", [1, 5, 6])
def test_response_invalid_stops_without_retry_limit_reclassification(reason, attempt):
    """受付の成否を確認できない応答違反は、回数によらず明示的に停止する。"""
    error = PublishResponseInvalidError(reason=reason)
    assert decide_publish_retry(
        error, attempt_count=attempt, jitter=0.5
    ) == NonRetryable(NonRetryableReason.NON_RETRYABLE_FAILURE)


def test_retry_delays_cover_every_attempt_before_shared_limit():
    """確保処理と共有する上限まで、待ち時間と上限到達の判断が整合する。"""
    from app.outbox.delivery.retry_policy import _RETRY_DELAYS, MAX_PUBLISH_ATTEMPTS

    assert MAX_PUBLISH_ATTEMPTS == 5
    assert len(_RETRY_DELAYS) == MAX_PUBLISH_ATTEMPTS - 1
    for attempt in range(1, MAX_PUBLISH_ATTEMPTS):
        assert decide_publish_retry(
            transport(), attempt_count=attempt, jitter=0.5
        ) == Retryable(RetryDelay(timedelta(seconds=_RETRY_DELAYS[attempt - 1])))
    assert decide_publish_retry(
        transport(), attempt_count=MAX_PUBLISH_ATTEMPTS, jitter=0.5
    ) == NonRetryable(NonRetryableReason.RETRY_EXHAUSTED)


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(seconds=-1), None])
def test_retryable_requires_validated_delay(delay):
    """判断結果は検証済みの待ち時間を保持する。"""
    with pytest.raises(TypeError):
        Retryable(delay)


@pytest.mark.parametrize("reason", ["non_retryable_failure", "", "custom_reason", None])
def test_non_retryable_requires_reason_enum(reason):
    """値が一致する文字列でもEnumへの暗黙変換は行わない。"""
    with pytest.raises(TypeError):
        NonRetryable(reason)
