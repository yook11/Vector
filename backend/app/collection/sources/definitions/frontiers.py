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
from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.reader.rss_reader import RssEntry
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
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


async def frontiers_entries(
    tools: FetchTools,
    *,
    source_name: str,
    endpoint_url: str,
) -> AsyncIterator[FetchedArticle]:
    """Frontiers Media journal RSS の取得共通処理。"""
    entries = await tools.rss.fetch(
        endpoint_url=endpoint_url,
        source_name=source_name,
        parse_mode="bytes",
    )
    for entry in entries:
        yield FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=_strip_html(_pick_body(entry)) or None,
            published_at=entry.published,
        )


class FrontiersAISource:
    """Frontiers in Artificial Intelligence。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Artificial Intelligence")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/artificial-intelligence/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )


class FrontiersRoboticsAISource:
    """Frontiers in Robotics and AI。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Robotics and AI")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/robotics-and-ai/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )


class FrontiersEnergyResearchSource:
    """Frontiers in Energy Research。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Energy Research")
    endpoint_url: ClassVar[str] = (
        "https://www.frontiersin.org/journals/energy-research/rss"
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )


class FrontiersMaterialsSource:
    """Frontiers in Materials。"""

    name: ClassVar[SourceName] = SourceName("Frontiers in Materials")
    endpoint_url: ClassVar[str] = "https://www.frontiersin.org/journals/materials/rss"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return frontiers_entries(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )
