"""補完工程の原因と具体的な理由から再試行・終了と待機時刻を決める。"""

from datetime import datetime
from typing import assert_never

from pydantic import ValidationError

from app.collection.article_completion.consumer_result import (
    NoRetryCompletion,
    RetryCompletion,
)
from app.collection.article_completion.errors import (
    ArticleBuildRejectedError,
    ArticleCompletionError,
    FetchDeadlineExceededError,
    ResponseSizeLimitExceededError,
    RobotsDisallowedError,
)
from app.collection.article_completion.html_extraction import (
    HtmlExtractionError,
    HtmlExtractionFailureReason,
)
from app.collection.external_fetch_failure import (
    NonRetryableFetchFailure,
    RetryableFetchFailure,
    classify_external_fetch_failure,
)
from app.http.destination_policy import DestinationRejectedError
from app.http.errors import HttpResponseError, HttpTransportError


def classify_completion_failure(
    exc: ArticleCompletionError, *, now: datetime
) -> RetryCompletion | NoRetryCompletion:
    match exc:
        case ArticleCompletionError(
            cause=HttpResponseError()
            | HttpTransportError()
            | DestinationRejectedError() as cause
        ):
            match classify_external_fetch_failure(cause, now=now):
                case RetryableFetchFailure(
                    code=code, retry_at=retry_at, requires_investigation=investigate
                ):
                    return RetryCompletion(
                        error=exc,
                        code=code,
                        retry_at=retry_at,
                        requires_investigation=investigate,
                    )
                case NonRetryableFetchFailure(code=code):
                    return NoRetryCompletion(cause=exc, code=code)
        case ArticleCompletionError(
            cause=ArticleBuildRejectedError()
            | RobotsDisallowedError()
            | ResponseSizeLimitExceededError() as cause
        ):
            return NoRetryCompletion(cause=exc, code=cause.CODE)
        case ArticleCompletionError(cause=FetchDeadlineExceededError() as cause):
            return RetryCompletion(error=exc, code=cause.CODE)
        case ArticleCompletionError(cause=HtmlExtractionError(reason=reason)):
            match reason:
                case (
                    HtmlExtractionFailureReason.CONTENT_TYPE_UNSUPPORTED
                    | HtmlExtractionFailureReason.RESULT_EMPTY
                ):
                    return NoRetryCompletion(cause=exc, code=reason.value)
                case (
                    HtmlExtractionFailureReason.EXTRACTOR_RAISED
                    | HtmlExtractionFailureReason.RESULT_TYPE_MISMATCH
                    | HtmlExtractionFailureReason.HTML_DECODE_FAILED
                    | HtmlExtractionFailureReason.MATERIAL_CONVERSION_FAILED
                ):
                    return RetryCompletion(
                        error=exc, code=reason.value, requires_investigation=True
                    )
                case _:
                    assert_never(reason)
        case ArticleCompletionError(cause=ValidationError()):
            return RetryCompletion(
                error=exc, code="validation_error", requires_investigation=True
            )

    return RetryCompletion(error=exc, code="unknown", requires_investigation=True)
