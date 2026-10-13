"""``FetchedArticle.from_raw`` の受け付け条件のユニットテスト (DB 非依存)。

取得材料 ``RawArticle`` を、記事 URL と空でない title を持つ ``FetchedArticle`` に
するか、受け付けない理由の例外を送出するかを確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.article_acquisition.fetched_article import (
    FetchedArticle,
    FetchedArticleTitleMissingError,
)
from app.collection.article_acquisition.raw_article import RawArticle
from app.collection.domain.article_url import ArticleUrlInvalidError

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _raw(
    *,
    title: str = "Hello World",
    url: str = "https://example.com/articles/hello-world",
) -> RawArticle:
    return RawArticle(title=title, url=url, body="body", published_at=_PUBLISHED)


def test_accepts_raw_article_as_article_url_and_trimmed_title() -> None:
    """追跡用パラメータ付き URL と空白付き title は、記事 URL と整えた title になる。"""
    fetched = FetchedArticle.from_raw(
        _raw(title="  Hello World  ", url="https://example.com/a?utm_source=feed#top")
    )
    assert (str(fetched.url), fetched.title, fetched.body, fetched.published_at) == (
        "https://example.com/a",
        "Hello World",
        "body",
        _PUBLISHED,
    )


def test_caps_title_at_500_chars() -> None:
    """500 字を超える title は 500 字に切り詰めて受け付ける。"""
    fetched = FetchedArticle.from_raw(_raw(title="a" * 600))
    assert fetched.title == "a" * 500


def test_accepts_private_ip_url_leaving_destination_to_fetch() -> None:
    """非公開IPのURLは受け付けで棄却せず、宛先の拒否は取得時の送信境界に任せる。"""
    fetched = FetchedArticle.from_raw(_raw(url="http://127.0.0.1/secret"))
    assert str(fetched.url) == "http://127.0.0.1/secret"


@pytest.mark.parametrize("title", ["", "   ", "\n\t  "])
def test_rejects_empty_title(title: str) -> None:
    """空白だけの title は、title 欠落の例外で棄却される。"""
    with pytest.raises(FetchedArticleTitleMissingError):
        FetchedArticle.from_raw(_raw(title=title))


@pytest.mark.parametrize("url", ["", "   ", "\n\t  "])
def test_rejects_empty_url_as_url_empty(url: str) -> None:
    """空白だけの URL は、``url_empty`` の URL 不正の例外で棄却される。"""
    with pytest.raises(ArticleUrlInvalidError) as excinfo:
        FetchedArticle.from_raw(_raw(url=url))
    assert excinfo.value.reason.value == "url_empty"


def test_rejects_non_http_url_as_url_not_http() -> None:
    """http/https 以外の URL は、``url_not_http`` の URL 不正の例外で棄却される。"""
    with pytest.raises(ArticleUrlInvalidError) as excinfo:
        FetchedArticle.from_raw(_raw(url="javascript:alert(1)"))
    assert excinfo.value.reason.value == "url_not_http"


def test_rejects_overlong_url_as_url_too_long() -> None:
    """2048 字超の URL は、``url_too_long`` の URL 不正の例外で棄却される。"""
    with pytest.raises(ArticleUrlInvalidError) as excinfo:
        FetchedArticle.from_raw(_raw(url="https://example.com/" + "a" * 2050))
    assert excinfo.value.reason.value == "url_too_long"


def test_empty_url_takes_precedence_over_empty_title() -> None:
    """URL と title が両方空なら、URL の不正として棄却される。"""
    with pytest.raises(ArticleUrlInvalidError) as excinfo:
        FetchedArticle.from_raw(_raw(url="", title=""))
    assert excinfo.value.reason.value == "url_empty"


def test_invalid_url_rejection_chains_web_url_cause() -> None:
    """URL 不正の例外は WebUrl 由来の ValueError を原因に連鎖し、監査が辿れる。"""
    with pytest.raises(ArticleUrlInvalidError) as excinfo:
        FetchedArticle.from_raw(_raw(url="ftp://example.com/secret"))
    assert isinstance(excinfo.value.__cause__, ValueError)
