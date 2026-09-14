"""補完工程の終了判断と、相手が指定した待機期限を保証する。"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.collection.article_completion.consumer_failure_classification import (
    CloseArticleCompletion,
    RetryArticleCompletion,
    classify_completion_failure,
)
from app.collection.article_completion.errors import (
    ArticleCompletionRejectedError,
    ArticleContentQualityError,
    ArticleContentTypeError,
    ArticleExtractionCrashedError,
    ArticleExtractionCrashReason,
    ArticleExtractionEmptyError,
    FetchDeadlineExceededError,
    FetchResource,
    ResponseSizeBasis,
    ResponseSizeLimitExceededError,
    RobotsDisallowedError,
)
from app.collection.domain.analyzable_article import AnalyzableArticleDefect as Defect
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


@pytest.mark.parametrize(
    ("status", "expected_result", "investigate"),
    [
        (302, CloseArticleCompletion, False),
        (400, CloseArticleCompletion, False),
        (401, CloseArticleCompletion, False),
        (403, CloseArticleCompletion, False),
        (404, CloseArticleCompletion, False),
        (407, RetryArticleCompletion, True),
        (408, RetryArticleCompletion, False),
        (410, CloseArticleCompletion, False),
        (421, RetryArticleCompletion, False),
        (425, RetryArticleCompletion, True),
        (429, RetryArticleCompletion, False),
        (451, CloseArticleCompletion, False),
        (499, CloseArticleCompletion, False),
        (500, RetryArticleCompletion, False),
        (501, CloseArticleCompletion, False),
        (502, RetryArticleCompletion, False),
        (503, RetryArticleCompletion, False),
        (504, RetryArticleCompletion, False),
        (505, CloseArticleCompletion, False),
        (507, RetryArticleCompletion, False),
        (511, RetryArticleCompletion, True),
        (599, RetryArticleCompletion, False),
        (100, RetryArticleCompletion, True),
        (200, RetryArticleCompletion, True),
        (600, RetryArticleCompletion, True),
    ],
)
def test_http_response_decision(
    status: int,
    expected_result: type[RetryArticleCompletion] | type[CloseArticleCompletion],
    investigate: bool,
) -> None:
    """補完工程で合意した応答分類に従い、環境側の問題では記事を閉じない。"""
    exc = HttpResponseError(status_code=status, received_at=_RECEIVED)
    decision = classify_completion_failure(exc, now=_NOW)
    assert isinstance(decision, expected_result)
    assert decision.requires_investigation is investigate


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
def test_transport_failure_never_becomes_article_rejection(
    failure: HttpTransportFailure, investigate: bool
) -> None:
    """通信段階の失敗を記事のHTTP応答による終了へ読み替えない。"""
    decision = classify_completion_failure(
        HttpTransportError(failure=failure), now=_NOW
    )
    assert isinstance(decision, RetryArticleCompletion)
    assert decision.requires_investigation is investigate


@pytest.mark.parametrize(
    "exc",
    [
        HostBlockedError(),
        RobotsDisallowedError(),
        ResponseSizeLimitExceededError(
            resource=FetchResource.ARTICLE_PAGE,
            limit_bytes=1024,
            observed_bytes=1025,
            size_basis=ResponseSizeBasis.RECEIVED_DECODED_BODY,
        ),
        ArticleContentTypeError(content_type="application/json"),
        ArticleExtractionEmptyError(),
        ArticleContentQualityError(body_length=0, title_present=False),
        ArticleCompletionRejectedError(defects=(Defect.BODY_TOO_SHORT,)),
    ],
)
def test_known_completion_rejection_closes(exc: Exception) -> None:
    """確認済みの取得禁止や補完条件不足だけを終了へ進める。"""
    decision = classify_completion_failure(exc, now=_NOW)
    assert isinstance(decision, CloseArticleCompletion)
    assert decision.requires_investigation is False


@pytest.mark.parametrize(
    ("defects", "unmapped"),
    [
        ((Defect.BODY_TOO_SHORT, Defect.UNMAPPED_VALIDATION_ERROR), ()),
        ((Defect.BODY_TOO_SHORT,), ("source_url:new_constraint",)),
        ((), ()),
    ],
)
def test_uncertain_build_rejection_preserves_reasons_and_retries(
    defects: tuple[Defect, ...], unmapped: tuple[str, ...]
) -> None:
    """未分類が混在する構築拒否は情報を失わず、終了より調査と再試行を優先する。"""
    exc = ArticleCompletionRejectedError(defects=defects, unmapped=unmapped)
    decision = classify_completion_failure(exc, now=_NOW)
    assert isinstance(decision, RetryArticleCompletion)
    assert decision.requires_investigation is True
    assert decision.code == "article_completion_rejected"
    assert exc.defects is defects
    assert exc.unmapped is unmapped


@pytest.mark.parametrize(
    ("exc", "code", "investigate"),
    [
        (
            FetchDeadlineExceededError(
                resource=FetchResource.ROBOTS_TXT, limit_seconds=10
            ),
            "article_fetch_deadline_exceeded",
            False,
        ),
        (
            ArticleExtractionCrashedError(
                reason=ArticleExtractionCrashReason.EXCEPTION
            ),
            "article_extraction_crashed",
            True,
        ),
        (
            ArticleExtractionCrashedError(
                reason=ArticleExtractionCrashReason.UNEXPECTED_RESULT
            ),
            "article_extraction_crashed",
            True,
        ),
        (TypeError("unexpected implementation failure"), "unknown", True),
    ],
)
def test_execution_failure_preserves_cause_and_retries(
    exc: Exception, code: str, investigate: bool
) -> None:
    """実行障害を再試行へ渡し、調査に必要な元の例外チェーンを保持する。"""
    cause = ValueError("original failure")
    try:
        raise exc from cause
    except Exception as original:
        traceback = original.__traceback__
        decision = classify_completion_failure(original, now=_NOW)
        assert isinstance(decision, RetryArticleCompletion)
        assert decision.code == code
        assert decision.requires_investigation is investigate
        assert original.__cause__ is cause
        assert original.__traceback__ is traceback


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
    decision = classify_completion_failure(exc, now=_NOW)
    assert isinstance(decision, RetryArticleCompletion)
    assert decision.retry_at == expected
    if decision.retry_at is not None:
        assert decision.retry_at.tzinfo is UTC
    assert exc.retry_after == value


@pytest.mark.parametrize("status", [403, 501])
def test_retry_after_does_not_override_close(status: int) -> None:
    """待機ヘッダーがあっても補完終了の判断を再試行へ戻さない。"""
    exc = HttpResponseError(
        status_code=status, received_at=_RECEIVED, retry_after="120"
    )
    decision = classify_completion_failure(exc, now=_NOW)
    assert isinstance(decision, CloseArticleCompletion)
