"""Anthropic 用 Source。

Anthropic は RSS を一切提供せず ``/sitemap.xml`` のみ利用可能。sitemap には
title が無いため URL slug を title に詰める。robots.txt は ``Allow: /`` で
``Sitemap:`` を明示。attribution_label は source name ``"Anthropic"`` を使う
(DB 行は alembic ``o3_add_anthropic`` で seed)。

収集スコープ (``is_collectable_anthropic_url``): Anthropic が採るのは
``/news/`` 配下のみ。about / pricing 等は妥当な URL を持つが**ソースが
意図的に採らない対象外データ** (spec 第4責務 = 収集スコープ宣言。対象外 ≠
変換失敗 ≠ 構造的非記事)。loc/lastmod parse は ``SitemapReader`` の責務。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlparse

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.reader.sitemap_reader import SitemapEntry
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.sources.article_completion_policy import (
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
)
from app.shared.value_objects.source_name import SourceName

_SOURCE_NAME = "Anthropic"
_NEWS_PATH_PREFIX = "/news/"
_MAX_ENTRIES = 30
_EPOCH = datetime.min.replace(tzinfo=UTC)


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def is_collectable_anthropic_url(entry: SitemapEntry) -> bool:
    """Anthropic が収集対象として宣言する URL か (純粋なスコープ述語)。

    ``/news/`` 配下以外 (about / pricing / loc 欠落) は変換失敗ではなく
    「ソースが意図的に採らない対象外データ」。
    """
    return urlparse(entry.loc).path.startswith(_NEWS_PATH_PREFIX)


def to_fetched_article(entry: SitemapEntry) -> FetchedArticle:
    """in-scope な ``SitemapEntry`` → ``FetchedArticle`` の純粋 total 写像。

    title は URL slug (sitemap に title が無いため)。slug 空は source 名に
    fallback。drop / None を返さず素通し (converter が可視化)。
    """
    return FetchedArticle(
        title=_slug_from_url(entry.loc) or _SOURCE_NAME,
        url=entry.loc,
        body=None,
        published_at=entry.lastmod,
    )


class AnthropicSource:
    """Anthropic news の Source。

    ``URL_PATH_PREFIX`` 以外を収集スコープ外として除外し、lastmod 降順
    sort 後に ``MAX_ENTRIES`` 件で打ち切る。
    """

    name: ClassVar[SourceName] = SourceName(_SOURCE_NAME)
    endpoint_url: ClassVar[str] = "https://www.anthropic.com/sitemap.xml"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.sitemap
    completion_policy: ClassVar[ArticleCompletionPolicy] = HTML_TITLE_POLICY

    URL_PATH_PREFIX: ClassVar[str] = _NEWS_PATH_PREFIX
    MAX_ENTRIES: ClassVar[int] = _MAX_ENTRIES

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        reader = tools.sitemap()
        entries = await reader.fetch(url=cls.endpoint_url, source_name=str(cls.name))
        in_scope = [e for e in entries if is_collectable_anthropic_url(e)]
        in_scope.sort(key=lambda e: e.lastmod or _EPOCH, reverse=True)
        for entry in in_scope[: cls.MAX_ENTRIES]:
            yield to_fetched_article(entry)
