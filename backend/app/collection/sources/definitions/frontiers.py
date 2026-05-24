"""Frontiers Media (Open Access journals) の取得 (機構 + Source 定義)。

Frontiers Media は Open Access の学術出版社で、全 journal が同形式の RSS
を提供する (RSS 2.0、``<title>`` / ``<link>`` / ``<pubDate>``、本文は
``<description>`` の abstract 全文。license は全 journal CC BY 4.0)。
全 journal の license が統一されているため attribution は news_sources 行
で扱う。
"""

from __future__ import annotations

import html
import re
from typing import ClassVar

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.reader.rss_reader import RssEntry
from app.collection.article_collection.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.shared.value_objects.source_name import SourceName

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化 (body 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """``content_encoded`` と ``summary`` の長い方を本文として採用する。

    Frontiers は ``content`` が空 / 欠落で ``summary`` (description) に
    abstract 全文を載せる。
    """
    content_encoded = entry.content_encoded or ""
    summary = entry.summary or ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


def to_fetched_article(entry: RssEntry) -> FetchedArticle:
    """``RssEntry`` → ``FetchedArticle`` の純粋 total 写像 (abstract を body 採用)。"""
    return FetchedArticle(
        title=entry.title,
        url=entry.link,
        body=_strip_html(_pick_body(entry)) or None,
        published_at=entry.published,
    )


async def frontiers_read(
    tools: ReaderTools,
    *,
    source_name: str,
    endpoint_url: str,
) -> list[RssEntry]:
    """Frontiers Media journal RSS の取得共通処理。"""
    return await tools.rss.fetch(
        endpoint_url=endpoint_url,
        source_name=source_name,
        parse_mode="bytes",
    )


class FrontiersAISource(BaseArticleSource):
    """Frontiers in Artificial Intelligence。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Artificial Intelligence")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/artificial-intelligence/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await frontiers_read(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return to_fetched_article(entry)


class FrontiersRoboticsAISource(BaseArticleSource):
    """Frontiers in Robotics and AI。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Robotics and AI")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/robotics-and-ai/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await frontiers_read(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return to_fetched_article(entry)


class FrontiersEnergyResearchSource(BaseArticleSource):
    """Frontiers in Energy Research。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Energy Research")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/energy-research/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await frontiers_read(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return to_fetched_article(entry)


class FrontiersMaterialsSource(BaseArticleSource):
    """Frontiers in Materials。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Materials")
    endpoint_url: ClassVar[str] = "https://www.frontiersin.org/journals/materials/rss"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await frontiers_read(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return to_fetched_article(entry)
