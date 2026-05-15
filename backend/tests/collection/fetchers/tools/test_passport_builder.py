"""``try_build_passport`` のユニットテスト (DB 非依存)。

本 builder は per-source の RSS body 信用 policy (= ``body_candidate`` を
渡すか ``None`` を渡すか) と、ReadyForArticle / IncompleteArticle / drop の
最終分岐を担う。テストは「分岐契約」だけに集中し、ソース固有の HTML strip や
tag filter は per-source 単体テスト側の責務とする。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.collection.article.domain.article import (
    _ARTICLE_BODY_MAX_LENGTH,
    _ARTICLE_BODY_MIN_LENGTH,
    ReadyForArticle,
)
from app.collection.fetchers.tools.passport_builder import try_build_passport
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_VALID_LINK = "https://example.com/articles/hello-world"
_VALID_TITLE = "Hello World"
_VALID_BODY = "x" * _ARTICLE_BODY_MIN_LENGTH


def _call(**overrides):
    base = dict(
        title=_VALID_TITLE,
        link=_VALID_LINK,
        body_candidate=_VALID_BODY,
        published_hint=_PUBLISHED,
        source_id=1,
    )
    base.update(overrides)
    return try_build_passport(**base)


def test_returns_ready_when_body_and_published_present() -> None:
    result = _call()
    assert isinstance(result, ReadyForArticle)
    assert result.body == _VALID_BODY
    assert result.published_at.value == _PUBLISHED


def test_returns_incomplete_when_body_candidate_is_none() -> None:
    """Pattern H 固定 (RSS body 不信用) を表現する経路。"""
    result = _call(body_candidate=None)
    assert isinstance(result, IncompleteArticle)
    assert result.published_at_hint is not None
    assert result.published_at_hint.value == _PUBLISHED


def test_returns_incomplete_when_body_too_short() -> None:
    """Pattern R 系で teaser しか取れなかったとき Incomplete fallback。"""
    result = _call(body_candidate="x" * (_ARTICLE_BODY_MIN_LENGTH - 1))
    assert isinstance(result, IncompleteArticle)


def test_returns_incomplete_when_body_exceeds_max_length() -> None:
    result = _call(body_candidate="x" * (_ARTICLE_BODY_MAX_LENGTH + 1))
    assert isinstance(result, IncompleteArticle)


def test_returns_incomplete_when_published_missing() -> None:
    """body 揃っても published 欠落なら Incomplete fallback。"""
    result = _call(published_hint=None)
    assert isinstance(result, IncompleteArticle)
    assert result.published_at_hint is None


def test_drops_naive_published_silently_and_falls_back_to_incomplete() -> None:
    """tz-naive datetime は PublishedAt 構造違反 → published 不採用。"""
    naive = datetime(2026, 5, 1, 12, 0)
    result = _call(published_hint=naive)
    assert isinstance(result, IncompleteArticle)
    assert result.published_at_hint is None


def test_accepts_non_utc_published_hint() -> None:
    """PublishedAt は tz-aware ならば UTC 以外でも受理する。"""
    jst = timezone(timedelta(hours=9))
    result = _call(published_hint=datetime(2026, 5, 1, 21, 0, tzinfo=jst))
    assert isinstance(result, ReadyForArticle)


@pytest.mark.parametrize("title", [None, "", "   ", "\n\t  "])
def test_drops_when_title_is_empty(title: str | None) -> None:
    assert _call(title=title) is None


def test_trims_title_whitespace_and_caps_500_chars() -> None:
    long_title = "  " + ("a" * 600) + "  "
    result = _call(title=long_title)
    assert isinstance(result, ReadyForArticle)
    assert result.title == "a" * 500


@pytest.mark.parametrize("link", [None, ""])
def test_drops_when_link_is_empty(link: str | None) -> None:
    assert _call(link=link) is None


def test_drops_when_link_is_private_ip_literal() -> None:
    """SSRF 防御 (SafeUrl): IP リテラルが private/loopback なら drop。"""
    assert _call(link="http://127.0.0.1/secret") is None


def test_drops_when_link_is_not_http_scheme() -> None:
    assert _call(link="javascript:alert(1)") is None
