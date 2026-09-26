"""取得の失敗を、再試行で変わりうるものだけ再配信する判断を保証する。"""

from datetime import UTC, datetime

import pytest

from app.collection.article_acquisition.consumer_failure_classification import (
    NoRetryAcquisition,
    RetryAcquisition,
    classify_acquisition_failure,
)
from app.collection.article_acquisition.errors import RssFeedErrors, RssFeedFailure
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.db.errors import DatabaseTimeoutError, DatabaseTimeoutErrorReason
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def _read_error() -> UnreadableResponseError:
    return UnreadableResponseError(
        reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="feed"
    )


def _response_error(status_code: int) -> HttpResponseError:
    return HttpResponseError(status_code=status_code, received_at=_NOW)


def _feeds(*errors: Exception) -> RssFeedErrors:
    return RssFeedErrors(
        [
            RssFeedFailure(feed_url=f"https://example.com/{index}", error=error)
            for index, error in enumerate(errors)
        ]
    )


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(_response_error(503), id="retryable_response"),
        pytest.param(
            HttpTransportError(
                failure=HttpTransportFailure(
                    HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                )
            ),
            id="transport_failure",
        ),
        pytest.param(_feeds(_read_error(), _response_error(429)), id="rss_any_retry"),
        pytest.param(
            DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.STATEMENT_TIMEOUT),
            id="database_failure",
        ),
        pytest.param(RuntimeError("unexpected"), id="unexpected_failure"),
    ],
)
def test_failure_that_may_change_or_is_not_understood_is_redelivered(
    exc: Exception,
) -> None:
    """再試行で変わりうる失敗と、DB障害・想定外の失敗は再配信する。"""
    assert classify_acquisition_failure(exc, now=_NOW) == RetryAcquisition(exc)


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(_response_error(403), id="non_retryable_response"),
        pytest.param(HostBlockedError("private IP literal"), id="host_blocked"),
        pytest.param(_read_error(), id="unreadable_response"),
        pytest.param(
            _feeds(_read_error(), _response_error(404)), id="rss_all_no_retry"
        ),
    ],
)
def test_failure_that_retry_cannot_change_is_not_redelivered(exc: Exception) -> None:
    """再試行しても変わらない失敗は再配信せず、次の定期投入に任せる。"""
    assert classify_acquisition_failure(exc, now=_NOW) == NoRetryAcquisition(exc)
