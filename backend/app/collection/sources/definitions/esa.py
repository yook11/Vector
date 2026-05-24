"""ESA Djangoplicity 規格 RSS の取得 (機構 + Source 定義)。

ESA/Hubble / ESA/Webb は同じ Djangoplicity News module で、RSS は同形式
(RSS 2.0、``<title>`` / ``<link>`` / ``<pubDate>`` / ``<description>``、
``<author>`` / ``<media:*>`` は出ない)。本文は RSS に無く HTML 詳細ページ
側にあるため ``body=None`` で渡す。

写像 (``to_fetched_article``) は純粋 total で degenerate (空 title / 空
link / published 不在) を drop せず素通しし、converter が ``MISSING_TITLE``
/ ``MISSING_URL`` として可視化する (failure-visibility)。500 字 cap は
converter の ``ARTICLE_TITLE_MAX_LENGTH`` 一元、写像は複製しない。
"""

from __future__ import annotations

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


def to_fetched_article(entry: RssEntry) -> FetchedArticle:
    """``RssEntry`` → ``FetchedArticle`` の純粋 total 写像。

    Pattern H のため ``body`` は RSS 本文を採らず ``None`` 固定 (HTML 詳細
    ページ側で補完する)。
    """
    return FetchedArticle(
        title=entry.title,
        url=entry.link,
        body=None,
        published_at=entry.published,
    )


async def djangoplicity_read(
    tools: ReaderTools,
    *,
    source_name: str,
    endpoint_url: str,
) -> list[RssEntry]:
    """ESA Djangoplicity News module RSS の取得共通処理。"""
    return await tools.rss.fetch(
        endpoint_url=endpoint_url,
        source_name=source_name,
        parse_mode="bytes",
    )


class ESAHubbleSource(BaseArticleSource):
    """ESA/Hubble news (Djangoplicity RSS)。"""

    name: ClassVar[SourceName] = SourceName("ESA/Hubble")
    endpoint_url: ClassVar[str] = "https://esahubble.org/news/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await djangoplicity_read(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return to_fetched_article(entry)


class ESAWebbSource(BaseArticleSource):
    """ESA/Webb news (Djangoplicity RSS)。"""

    name: ClassVar[SourceName] = SourceName("ESA/Webb")
    endpoint_url: ClassVar[str] = "https://esawebb.org/news/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await djangoplicity_read(
            tools, source_name=str(cls.name), endpoint_url=cls.endpoint_url
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return to_fetched_article(entry)
