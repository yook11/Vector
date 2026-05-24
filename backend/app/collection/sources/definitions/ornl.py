"""ORNL (Oak Ridge National Laboratory) 用 Source。

RSS / Atom / sitemap.xml を提供しないため ``/news`` listing ページ
(``https://www.ornl.gov/news``) から記事 URL を列挙する。listing には title
が無いため URL slug を title に詰める。detail link は
``//a[starts-with(@href, "/news/")]`` で抽出 (xpath は Source 宣言値を
``HtmlListingReader`` に渡す)。License は U.S. Government work、
attribution_label = "ORNL · DOE"。

収集スコープ (``is_collectable_ornl_url``): ``EXCLUDED_PATHS`` の category
landing は妥当な URL を持つが**ソースが意図的に採らない対象外データ**
(spec 第4責務 = 収集スコープ宣言。対象外 ≠ 変換失敗 ≠ 構造的非記事)。
href 抽出は ``HtmlListingReader`` の責務、相対→絶対 URL 化は Source 純写像。
``select`` で listing 内 URL dedup + ``MAX_ENTRIES`` 件で打ち切る。
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urljoin, urlparse

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.reader.html_listing_reader import (
    HtmlListingEntry,
)
from app.collection.article_collection.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.shared.value_objects.source_name import SourceName

_SOURCE_NAME = "ORNL"
_DETAIL_LINK_XPATH = '//a[starts-with(@href, "/news/")]'
_DETAIL_URL_PREFIX = "https://www.ornl.gov"
# 2026-05-04 時点の実 listing で確認した category landing 6 件。
_EXCLUDED_PATHS = frozenset(
    {
        "/news/releases",
        "/news/features",
        "/news/researcher-profiles",
        "/news/story-tips",
        "/news/audio-spots",
        "/news/honors-and-awards",
    }
)
_MAX_ENTRIES = 30


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def _absolute_url(entry: HtmlListingEntry) -> str:
    """相対 href → 絶対 URL の純写像 (spec: 相対→URL 組立は Source mapping)。"""
    return urljoin(_DETAIL_URL_PREFIX, entry.href.strip())


def is_collectable_ornl_url(entry: HtmlListingEntry) -> bool:
    """ORNL が収集対象として宣言する URL か (純粋なスコープ述語)。

    ``EXCLUDED_PATHS`` の category landing は変換失敗ではなく「ソースが
    意図的に採らない対象外データ」。
    """
    return urlparse(_absolute_url(entry)).path not in _EXCLUDED_PATHS


def to_fetched_article(entry: HtmlListingEntry) -> FetchedArticle:
    """in-scope な ``HtmlListingEntry`` → ``FetchedArticle`` の純粋 total 写像。

    title は URL slug (listing に title が無いため)。listing は lastmod を
    持たないため ``published_at=None`` (HTML 抽出側で確定)。
    """
    url = _absolute_url(entry)
    return FetchedArticle(
        title=_slug_from_url(url) or _SOURCE_NAME,
        url=url,
        body=None,
        published_at=None,
    )


class ORNLSource(BaseArticleSource):
    """ORNL news listing 用 Source。

    ``EXCLUDED_PATHS`` denylist で category landing を収集スコープ外として
    除外し、``select`` で同一 listing 内 URL dedup + ``MAX_ENTRIES`` 件打ち切り。
    """

    name: ClassVar[SourceName] = SourceName(_SOURCE_NAME)
    endpoint_url: ClassVar[str] = "https://www.ornl.gov/news"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.listing
    completion_policy: ClassVar[ArticleCompletionPolicy] = HTML_TITLE_POLICY

    DETAIL_LINK_XPATH: ClassVar[str] = _DETAIL_LINK_XPATH
    DETAIL_URL_PREFIX: ClassVar[str] = _DETAIL_URL_PREFIX
    EXCLUDED_PATHS: ClassVar[frozenset[str]] = _EXCLUDED_PATHS
    MAX_ENTRIES: ClassVar[int] = _MAX_ENTRIES

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[HtmlListingEntry]:
        return await tools.html_listing().fetch(
            url=cls.endpoint_url,
            source_name=str(cls.name),
            detail_link_xpath=cls.DETAIL_LINK_XPATH,
        )

    @classmethod
    def in_scope(cls, entry: HtmlListingEntry) -> bool:
        return is_collectable_ornl_url(entry)

    @classmethod
    def select(cls, entries: list[HtmlListingEntry]) -> list[HtmlListingEntry]:
        """同一 URL dedup 後に ``MAX_ENTRIES`` 件で打ち切る (出現順を保つ)。"""
        seen: set[str] = set()
        result: list[HtmlListingEntry] = []
        for entry in entries:
            absolute = _absolute_url(entry)
            if absolute in seen:
                continue
            seen.add(absolute)
            result.append(entry)
            if len(result) >= cls.MAX_ENTRIES:
                break
        return result

    @classmethod
    def map_entry(cls, entry: HtmlListingEntry) -> FetchedArticle:
        return to_fetched_article(entry)
