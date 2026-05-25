"""Hacker News 用 Source (Algolia HN Search API)。

HN は RSS / Atom を持たず API ベース。API hit の ``url`` は外部の任意サイトを
指すため本文は HN 側で取得できず、body は HTML 抽出に委ねる。直近
``HN_SLIDING_WINDOW_SECONDS`` 秒以内に投稿された ``points > HN_MIN_POINTS``
のストーリーを全件取得する sliding window 設計。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.algolia_hn_reader import HackerNewsEntry
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.fetch_cadence import FetchCadence
from app.shared.value_objects.source_name import SourceName

# HN フェッチャー固有の運用値。Settings (環境変数経由) には載せない。
# 動的に切り替える運用要件が出た時点で config として宣言へ昇格させる。
HN_MIN_POINTS = 20
HN_HITS_PER_PAGE = 100
HN_SLIDING_WINDOW_SECONDS = 86400  # 24h


class HackerNewsSource(BaseArticleSource):
    """Hacker News 用 Source (Algolia Search API)。

    ``points>20`` の閾値は Algolia の server-side numericFilters で除外済。
    """

    name: ClassVar[SourceName] = SourceName("Hacker News")
    endpoint_url: ClassVar[str] = "https://hn.algolia.com/api/v1/search_by_date"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.api
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[HackerNewsEntry]:
        return await tools.hacker_news.search_recent_stories(
            source_name=str(cls.name),
            min_points=HN_MIN_POINTS,
            window_seconds=HN_SLIDING_WINDOW_SECONDS,
            hits_per_page=HN_HITS_PER_PAGE,
        )

    @classmethod
    def map_entry(cls, entry: HackerNewsEntry) -> FetchedArticle:
        """HN hit は外部サイトを指すため body は HTML 抽出に委ねる (None)。"""
        return FetchedArticle(
            title=entry.title or "",
            url=entry.url or "",
            body=None,
            published_at=entry.published,
        )
