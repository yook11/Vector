"""``FetchedArticleConversionError`` / ``ConversionReason`` /
``UnreadableResponseError`` の単体テスト。

DB / IO 非依存。例外が変換失敗 reason と観測スナップショットを構造化保持
すること、``code`` が単一の class 定数で安定すること、``ConversionReason``
の値が監査集計 key として安定な snake_case であること、read 段固有の
``UnreadableResponseError`` が接続 family と独立で CODE / 非空 message を
持つことを固定する。
"""

from __future__ import annotations

from app.audit.domain.event import Stage
from app.collection.article_acquisition.errors import (
    AcquisitionError,
    ConversionReason,
    FetchedArticleConversionError,
    SourceAcquisitionError,
    UnreadableResponseError,
)
from app.collection.external_fetch_errors import ExternalFetchError


def _external_fetch_subclasses(
    root: type[ExternalFetchError],
) -> set[type[ExternalFetchError]]:
    """``ExternalFetchError`` family を再帰的に列挙する。"""
    found: set[type[ExternalFetchError]] = set()
    for subclass in root.__subclasses__():
        found.add(subclass)
        found.update(_external_fetch_subclasses(subclass))
    return found


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
    """message 無しでも ``str(exc)`` が非空 (既定 message に CODE を合成)。

    service が ``SourceAcquisitionError(str(exc), code=exc.CODE)`` で監査に載せるため、
    空文字にならない構造保証が要る。
    """
    assert str(UnreadableResponseError()) == "read_unreadable_response"


def test_unreadable_response_error_explicit_message_takes_precedence() -> None:
    exc = UnreadableResponseError("sitemap parse error: Foo")
    assert str(exc) == "sitemap parse error: Foo"


def test_acquisition_error_is_stage_1_marker_base() -> None:
    assert AcquisitionError.STAGE is Stage.ACQUISITION


def test_source_acquisition_error_inherits_stage_marker_and_keeps_code() -> None:
    exc = SourceAcquisitionError("HTTP 403: VentureBeat", code="fetch_access_denied")

    assert isinstance(exc, AcquisitionError)
    assert exc.STAGE is Stage.ACQUISITION
    assert exc.code == "fetch_access_denied"
    assert str(exc) == "SourceAcquisitionError(code='fetch_access_denied')"


def test_external_fetch_error_family_has_no_stage_policy_attrs() -> None:
    """origin error family は Stage 固有 marker ではないことを固定する。"""
    assert not hasattr(ExternalFetchError, "STAGE")
    for cls in _external_fetch_subclasses(ExternalFetchError):
        assert not hasattr(cls, "STAGE")
        assert not hasattr(cls, "FAILURE_KIND")
        assert not hasattr(cls, "RETRYABILITY")
        assert not hasattr(cls, "FAILURE_ACTION")
