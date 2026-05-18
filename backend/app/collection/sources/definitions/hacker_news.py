"""Hacker News 用 Source (Algolia HN Search API)。

HN は RSS / Atom を持たず API ベース。API hit の ``url`` は外部の任意サイトを
指すため本文は HN 側で取得できず、body は HTML 抽出に委ねる。直近
``HN_SLIDING_WINDOW_SECONDS`` 秒以内に投稿された ``points > HN_MIN_POINTS``
のストーリーを全件取得する sliding window 設計。``url=None`` の hit
(Ask HN / Show HN テキスト投稿等) は対象外として除外する。dedup は下流の
``ON CONFLICT DO NOTHING`` に委ねる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.shared.value_objects.source_name import SourceName

# HN フェッチャー固有の運用値。Settings (環境変数経由) には載せない。
# 動的に切り替える運用要件が出た時点で config として宣言へ昇格させる。
HN_MIN_POINTS = 20
HN_HITS_PER_PAGE = 100
HN_SLIDING_WINDOW_SECONDS = 86400  # 24h

_TITLE_MAX_LENGTH = 500


def _parse_created_at(raw: str | None) -> PublishedAt | None:
    """Algolia の ``created_at`` (ISO 8601 + ``Z``) を UTC ``PublishedAt`` に変換。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


class HackerNewsSource:
    """Hacker News 用 Source (Algolia Search API)。

    ``url=None`` / 空 title の hit は対象外として除外する。``points>20`` の
    閾値は Algolia の server-side numericFilters で既に除外済。
    """

    name: ClassVar[SourceName] = SourceName("Hacker News")
    endpoint_url: ClassVar[str] = "https://hn.algolia.com/api/v1/search_by_date"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.api
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        hits = await tools.hacker_news.search_recent_stories(
            source_name=str(cls.name),
            min_points=HN_MIN_POINTS,
            window_seconds=HN_SLIDING_WINDOW_SECONDS,
            hits_per_page=HN_HITS_PER_PAGE,
        )
        for hit in hits:
            raw_url = hit.get("url")
            if not isinstance(raw_url, str) or not raw_url:
                continue  # Ask HN / text-only post: no external URL
            title = (hit.get("title") or "")[:_TITLE_MAX_LENGTH]
            if not title:
                continue  # title missing
            published = _parse_created_at(hit.get("created_at"))
            yield FetchedArticle(
                title=title,
                url=raw_url,
                body=None,
                published_at=published.value if published is not None else None,
            )
