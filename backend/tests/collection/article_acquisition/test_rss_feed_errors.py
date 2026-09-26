"""RSS失敗一覧が各フィードの元の例外を保持することを検証する。"""

from datetime import UTC, datetime

import pytest

from app.collection.article_acquisition.errors import RssFeedErrors, RssFeedFailure
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.http.errors import HttpResponseError


def test_failures_are_kept_as_given_without_raw_details_in_message() -> None:
    secret = "sk-" + "x" * 24
    failures = [
        RssFeedFailure(
            feed_url=f"https://example.com/feed?key={secret}",
            error=HttpResponseError(
                status_code=503, received_at=datetime(2026, 9, 26, tzinfo=UTC)
            ),
        ),
        RssFeedFailure(
            feed_url="https://example.com/other",
            error=UnreadableResponseError(
                f"private body {secret}",
                reason=UnreadableResponseReason.MALFORMED_CONTENT,
                response_format="feed",
            ),
        ),
    ]
    error = RssFeedErrors(failures)
    assert error.failures == tuple(failures)
    failures.clear()
    assert error.failure_count == 2
    assert secret not in str(error)
    assert "https://" not in str(error)


def test_empty_error_collection_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        RssFeedErrors([])
