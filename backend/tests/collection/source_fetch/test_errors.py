"""``FetchedArticleConversionError`` / ``ConversionReason`` の単体テスト。

DB / IO 非依存。例外が変換失敗 reason と観測スナップショットを構造化保持
すること、``code`` が単一の class 定数で安定すること、``ConversionReason``
の値が監査集計 key として安定な snake_case であることを固定する。
"""

from __future__ import annotations

from app.collection.source_fetch.errors import (
    ConversionReason,
    FetchedArticleConversionError,
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
