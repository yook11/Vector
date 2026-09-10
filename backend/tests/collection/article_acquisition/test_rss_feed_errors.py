"""RSS失敗一覧の保持・全体分類・安全な監査への変換を検証する。"""

import pytest

from app.audit.domain.payloads import AcquisitionPayload
from app.audit.failure_projection import Retryability, project_failure
from app.audit.stages.acquisition import _feed_failure_payloads
from app.collection.article_acquisition.errors import RssFeedErrors, RssFeedFailure
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.external_fetch_errors import (
    FetchOriginServerError,
    FetchResourceNotFoundError,
)


def _failure(retryable: bool) -> RssFeedFailure:
    error = (
        FetchOriginServerError(status_code=503, reason="unavailable")
        if retryable
        else FetchResourceNotFoundError(status_code=404, reason="not_found")
    )
    return RssFeedFailure(feed_url="https://example.com/feed", error=error)


@pytest.mark.parametrize(
    "flags", [(False,), (True,), (False, True), (True, False), (False, False)]
)
def test_retryability_uses_every_failure_and_keeps_source_handling(
    flags: tuple[bool, ...],
) -> None:
    failures = [_failure(flag) for flag in flags]
    error = RssFeedErrors(failures)
    projection = project_failure(error)
    assert projection.code == "rss_feed_errors"
    assert projection.failure_kind == "rss_feeds"
    assert projection.retryability is (
        Retryability.RETRYABLE if any(flags) else Retryability.NON_RETRYABLE
    )
    assert error.failures == tuple(failures)
    assert all(
        actual.error is expected.error
        for actual, expected in zip(error.failures, failures, strict=True)
    )
    failures.clear()
    assert error.failure_count == len(flags)


def test_empty_error_collection_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        RssFeedErrors([])


def test_audit_preserves_all_causes_without_raw_exception_messages() -> None:
    secret = "sk-" + "x" * 24
    cause = ValueError(f"raw response {secret}")
    origin = UnreadableResponseError(
        f"private body {secret}",
        reason=UnreadableResponseReason.MALFORMED_CONTENT,
        response_format="feed",
    )
    origin.__cause__ = cause
    error = RssFeedErrors(
        [
            RssFeedFailure(
                feed_url=f"https://example.com/feed?key={secret}", error=origin
            ),
            _failure(False),
        ]
    )
    assert error.RETRYABILITY is Retryability.NON_RETRYABLE
    assert secret not in str(error)
    assert "https://" not in str(error)
    payload = AcquisitionPayload(feed_failures=_feed_failure_payloads(error))
    serialized = payload.model_dump_json()
    assert secret not in serialized
    assert "private body" not in serialized
    assert "raw response" not in serialized
    assert len(payload.feed_failures) == 2
    first = payload.feed_failures[0]
    assert first.code == "read_malformed_content"
    assert first.error_chain == [
        f"{type(origin).__module__}.UnreadableResponseError",
        "builtins.ValueError",
    ]
    assert first.error_message == "read_malformed_content: feed"
    assert AcquisitionPayload.model_validate_json(serialized) == payload


def test_existing_payload_and_unrelated_error_have_no_feed_failures() -> None:
    assert (
        AcquisitionPayload.model_validate({"kind": "acquisition"}).feed_failures is None
    )
    assert _feed_failure_payloads(RuntimeError("unrelated")) is None
