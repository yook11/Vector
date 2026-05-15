"""``try_build_passport`` のユニットテスト (DB 非依存)。

``FetchedArticle`` 入力を passport (Ready / Incomplete / drop) に変換する
分岐契約を検証する。title / URL / body / published / ``prefer_html_title``
の各境界を網羅し、private helper ``_build_passport`` の判定順を固定する。

``prefer_html_title`` 関連の挙動 (Ready 経路ブロック / Incomplete 伝播) は
本ファイル固有のケース (case 13 / 14)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.collection.article.domain.article import (
    _ARTICLE_BODY_MAX_LENGTH,
    _ARTICLE_BODY_MIN_LENGTH,
    ReadyForArticle,
)
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.passport_builder import (
    try_build_passport,
)
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_VALID_URL = "https://example.com/articles/hello-world"
_VALID_TITLE = "Hello World"
_VALID_BODY = "x" * _ARTICLE_BODY_MIN_LENGTH

_BASE_FETCHED: dict = {
    "title": _VALID_TITLE,
    "url": _VALID_URL,
    "body": _VALID_BODY,
    "published_at": _PUBLISHED,
    "prefer_html_title": False,
}


def _call(**overrides):
    args = {**_BASE_FETCHED, **overrides}
    return try_build_passport(FetchedArticle(**args), source_id=1)


def test_returns_ready_when_body_and_published_present() -> None:
    result = _call()
    assert isinstance(result, ReadyForArticle)
    assert result.body == _VALID_BODY
    assert result.published_at.value == _PUBLISHED


def test_returns_incomplete_when_body_is_none() -> None:
    """RSS body 不信用 (旧 Pattern H 相当) を表現する経路。"""
    result = _call(body=None)
    assert isinstance(result, IncompleteArticle)
    assert result.published_at_hint is not None
    assert result.published_at_hint.value == _PUBLISHED


def test_returns_incomplete_when_body_too_short() -> None:
    result = _call(body="x" * (_ARTICLE_BODY_MIN_LENGTH - 1))
    assert isinstance(result, IncompleteArticle)


def test_returns_incomplete_when_body_exceeds_max_length() -> None:
    result = _call(body="x" * (_ARTICLE_BODY_MAX_LENGTH + 1))
    assert isinstance(result, IncompleteArticle)


def test_returns_incomplete_when_published_missing() -> None:
    result = _call(published_at=None)
    assert isinstance(result, IncompleteArticle)
    assert result.published_at_hint is None


def test_drops_naive_published_silently_and_falls_back_to_incomplete() -> None:
    """tz-naive datetime は PublishedAt 構造違反 → published 不採用。"""
    naive = datetime(2026, 5, 1, 12, 0)
    result = _call(published_at=naive)
    assert isinstance(result, IncompleteArticle)
    assert result.published_at_hint is None


def test_accepts_non_utc_published() -> None:
    jst = timezone(timedelta(hours=9))
    result = _call(published_at=datetime(2026, 5, 1, 21, 0, tzinfo=jst))
    assert isinstance(result, ReadyForArticle)


@pytest.mark.parametrize("title", ["", "   ", "\n\t  "])
def test_drops_when_title_is_empty(title: str) -> None:
    assert _call(title=title) is None


def test_trims_title_whitespace_and_caps_500_chars() -> None:
    long_title = "  " + ("a" * 600) + "  "
    result = _call(title=long_title)
    assert isinstance(result, ReadyForArticle)
    assert result.title == "a" * 500


def test_drops_when_url_is_empty() -> None:
    assert _call(url="") is None


def test_drops_when_url_is_private_ip_literal() -> None:
    """SSRF 防御 (SafeUrl): IP リテラルが private/loopback なら drop。"""
    assert _call(url="http://127.0.0.1/secret") is None


def test_drops_when_url_is_not_http_scheme() -> None:
    assert _call(url="javascript:alert(1)") is None


def test_prefer_html_title_propagates_to_incomplete_when_body_is_none() -> None:
    """``prefer_html_title=True`` flag が ``IncompleteArticle`` に伝播する。"""
    result = _call(body=None, prefer_html_title=True)
    assert isinstance(result, IncompleteArticle)
    assert result.prefer_html_title is True


def test_prefer_html_title_blocks_ready_path_even_when_body_and_published_present() -> (
    None
):
    """仮タイトル状態 (``prefer_html_title=True``) では body + published が
    揃っていても Ready 経路を止めて Incomplete に落ちる (安全弁)。"""
    result = _call(prefer_html_title=True)
    assert isinstance(result, IncompleteArticle)
    assert result.prefer_html_title is True
    assert result.published_at_hint is not None
    assert result.published_at_hint.value == _PUBLISHED
