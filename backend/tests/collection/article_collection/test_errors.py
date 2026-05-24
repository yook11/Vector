"""``FetchedArticleConversionError`` / ``ConversionReason`` /
``UnreadableResponseError`` の単体テスト。

DB / IO 非依存。例外が変換失敗 reason と観測スナップショットを構造化保持
すること、``code`` が単一の class 定数で安定すること、``ConversionReason``
の値が監査集計 key として安定な snake_case であること、read 段固有の
``UnreadableResponseError`` が接続 family と独立で CODE / 非空 message を
持つことを固定する。
"""

from __future__ import annotations

from app.collection.article_collection.errors import (
    ConversionReason,
    FetchedArticleConversionError,
    UnreadableResponseError,
)
from app.collection.external_fetch_errors import ExternalFetchError


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
    assert exc.code == "fetched_article_conversion_failed"
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

    service が ``SourceFetchError(str(exc), code=exc.CODE)`` で監査に載せるため、
    空文字にならない構造保証が要る。
    """
    assert str(UnreadableResponseError()) == "read_unreadable_response"


def test_unreadable_response_error_explicit_message_takes_precedence() -> None:
    exc = UnreadableResponseError("sitemap parse error: Foo")
    assert str(exc) == "sitemap parse error: Foo"
