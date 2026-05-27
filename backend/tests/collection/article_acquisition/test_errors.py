"""Stage 1 article acquisition 例外の単体テスト。"""

from __future__ import annotations

import pytest

from app.audit.domain.event import Stage
from app.audit.failure_projection import Retryability
from app.collection.article_acquisition.errors import (
    ACQUISITION_RECOVERABLE_FETCH_ERRORS,
    ACQUISITION_TERMINAL_FETCH_ERRORS,
    AcquisitionError,
    AcquisitionExternalFetchRecoverableError,
    AcquisitionExternalFetchTerminalError,
    AcquisitionUnreadableResponseError,
    ConversionReason,
    FetchedArticleConversionError,
    SourceAcquisitionError,
    UnreadableResponseError,
    map_origin_to_acquisition,
)
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchGatewayError,
)
from tests.collection.test_external_fetch_error_codes import (
    _CONSTRUCTION,
    _concrete_subclasses,
)


def _make(**overrides) -> FetchedArticleConversionError:
    kwargs = {
        "conversion_reason": ConversionReason.MISSING_TITLE,
        "source_name": "Example",
        "raw_url": "https://example.com/a",
        "has_title": True,
        "body_length": 12,
        "has_published_at": False,
    }
    kwargs.update(overrides)
    msg = f"conversion rejected: {kwargs['conversion_reason']}"
    return FetchedArticleConversionError(msg, **kwargs)


def test_code_is_stable_class_constant() -> None:
    exc = _make()
    assert exc.code == "article_conversion_rejected"
    assert exc.code == FetchedArticleConversionError.CODE


def test_carries_conversion_reason() -> None:
    exc = _make(conversion_reason=ConversionReason.UNEXPECTED_ERROR)
    assert exc.conversion_reason is ConversionReason.UNEXPECTED_ERROR


def test_carries_observation_snapshot() -> None:
    exc = _make(raw_url="https://x/y", has_title=False, body_length=None)
    assert exc.source_name == "Example"
    assert exc.raw_url == "https://x/y"
    assert exc.has_title is False
    assert exc.body_length is None
    assert exc.has_published_at is False


def test_message_is_deterministic_english() -> None:
    exc = _make(conversion_reason=ConversionReason.MISSING_TITLE)
    assert str(exc) == "conversion rejected: missing_title"


def test_conversion_reason_values_are_stable_snake_case() -> None:
    assert str(ConversionReason.MISSING_TITLE) == "missing_title"
    assert str(ConversionReason.INVALID_URL) == "invalid_url"
    assert str(ConversionReason.UNEXPECTED_ERROR) == "unexpected_error"


def test_is_an_exception() -> None:
    assert isinstance(_make(), Exception)


def test_unreadable_response_error_code_is_stable_read_prefixed() -> None:
    """CODE は接続 family の ``fetch_`` と別カテゴリと分かる ``read_`` prefix。"""
    assert UnreadableResponseError.CODE == "read_unreadable_response"


def test_unreadable_response_error_is_not_a_connection_error() -> None:
    """接続境界 ``ExternalFetchError`` family とは独立した別系統 (継承しない)。"""
    assert not issubclass(UnreadableResponseError, ExternalFetchError)


def test_unreadable_response_error_str_nonempty_without_message() -> None:
    """message 無しでも ``str(exc)`` が非空 (既定 message に CODE を合成)。"""
    assert str(UnreadableResponseError()) == "read_unreadable_response"


def test_unreadable_response_error_explicit_message_takes_precedence() -> None:
    exc = UnreadableResponseError("sitemap parse error: Foo")
    assert str(exc) == "sitemap parse error: Foo"


def test_acquisition_error_is_stage_1_marker_base() -> None:
    assert AcquisitionError.STAGE is Stage.ACQUISITION


def test_external_fetch_recoverable_marker_has_failure_attrs() -> None:
    origin = FetchGatewayError(status_code=502)
    exc = AcquisitionExternalFetchRecoverableError(origin_error=origin)

    assert isinstance(exc, AcquisitionError)
    assert isinstance(exc, SourceAcquisitionError)
    assert exc.STAGE is Stage.ACQUISITION
    assert exc.FAILURE_KIND == "external_fetch"
    assert exc.RETRYABILITY is Retryability.RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == "fetch_gateway_failure"
    assert exc.origin_error is origin
    assert str(exc) == (
        "AcquisitionExternalFetchRecoverableError(code='fetch_gateway_failure')"
    )


def test_external_fetch_terminal_marker_has_failure_attrs() -> None:
    origin = FetchAccessDeniedError(status_code=403, reason="forbidden")
    exc = AcquisitionExternalFetchTerminalError(origin_error=origin)

    assert exc.FAILURE_KIND == "external_fetch"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == "fetch_access_denied"
    assert exc.origin_error is origin
    assert str(exc) == (
        "AcquisitionExternalFetchTerminalError(code='fetch_access_denied')"
    )


def test_unreadable_response_marker_has_failure_attrs() -> None:
    origin = UnreadableResponseError("rss bozo")
    exc = AcquisitionUnreadableResponseError(origin_error=origin)

    assert exc.FAILURE_KIND == "unreadable_response"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == "read_unreadable_response"
    assert exc.origin_error is origin
    assert str(exc) == (
        "AcquisitionUnreadableResponseError(code='read_unreadable_response')"
    )


def test_acquisition_fetch_error_policy_tuples_cover_external_fetch_family() -> None:
    recoverable = set(ACQUISITION_RECOVERABLE_FETCH_ERRORS)
    terminal = set(ACQUISITION_TERMINAL_FETCH_ERRORS)

    assert recoverable | terminal == _concrete_subclasses(ExternalFetchError)
    assert recoverable.isdisjoint(terminal)


@pytest.mark.parametrize(
    ("cls", "kwargs"),
    list(_CONSTRUCTION.items()),
    ids=[c.__name__ for c in _CONSTRUCTION],
)
def test_map_origin_to_acquisition_preserves_origin_code(
    cls: type[ExternalFetchError],
    kwargs: dict[str, object],
) -> None:
    origin = cls(**kwargs)  # type: ignore[arg-type]

    marker = map_origin_to_acquisition(origin)

    assert marker.code == cls.CODE
    assert marker.origin_error is origin
    if cls in ACQUISITION_RECOVERABLE_FETCH_ERRORS:
        assert isinstance(marker, AcquisitionExternalFetchRecoverableError)
    else:
        assert isinstance(marker, AcquisitionExternalFetchTerminalError)


def test_map_origin_to_acquisition_maps_unreadable_response() -> None:
    origin = UnreadableResponseError("rss bozo")

    marker = map_origin_to_acquisition(origin)

    assert isinstance(marker, AcquisitionUnreadableResponseError)
    assert marker.code == "read_unreadable_response"
    assert marker.origin_error is origin


def test_external_fetch_error_family_has_no_stage_policy_attrs() -> None:
    """origin error family は Stage 固有 marker ではないことを固定する。"""
    assert not hasattr(ExternalFetchError, "STAGE")
    for cls in _concrete_subclasses(ExternalFetchError):
        assert not hasattr(cls, "STAGE")
        assert not hasattr(cls, "FAILURE_KIND")
        assert not hasattr(cls, "RETRYABILITY")
        assert not hasattr(cls, "FAILURE_ACTION")
