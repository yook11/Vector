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
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar
from urllib.parse import urljoin, urlparse

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    HTML_TITLE_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.reader.html_listing_reader import HtmlListingEntry
from app.collection.source_fetch.tools.fetch_tools import FetchTools
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


class ORNLSource:
    """ORNL news listing 用 Source。

    同一 listing 内 URL dedup、``EXCLUDED_PATHS`` denylist で category
    landing を収集スコープ外として除外、``MAX_ENTRIES`` 件で打ち切る。
    """

    name: ClassVar[SourceName] = SourceName(_SOURCE_NAME)
    endpoint_url: ClassVar[str] = "https://www.ornl.gov/news"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.listing
    completion_profile: ClassVar[SourceCompletionProfile] = HTML_TITLE_PROFILE

    DETAIL_LINK_XPATH: ClassVar[str] = _DETAIL_LINK_XPATH
    DETAIL_URL_PREFIX: ClassVar[str] = _DETAIL_URL_PREFIX
    EXCLUDED_PATHS: ClassVar[frozenset[str]] = _EXCLUDED_PATHS
    MAX_ENTRIES: ClassVar[int] = _MAX_ENTRIES

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        reader = tools.html_listing()
        entries = await reader.fetch(
            url=cls.endpoint_url,
            source_name=str(cls.name),
            detail_link_xpath=cls.DETAIL_LINK_XPATH,
        )
        seen: set[str] = set()
        emitted = 0
        for entry in entries:
            absolute = _absolute_url(entry)
            if absolute in seen:
                continue
            seen.add(absolute)
            if not is_collectable_ornl_url(entry):
                continue
            yield to_fetched_article(entry)
            emitted += 1
            if emitted >= cls.MAX_ENTRIES:
                break
