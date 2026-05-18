"""Hacker News 用 Source — Pattern H 設計 (Algolia HN Search API)。

HN はソース仕様が API ベース (Algolia HN Search API) で RSS / Atom feed を
持たないが、API hit の ``url`` は外部の任意サイトを指すため本文は HN 側で
取得できない。よって ``AnalyzableArticle`` invariant (body ≥ 50 chars) を
API 単独で満たせず、``ObservedArticle`` を yield し後段 ``extract_html_body``
task が trafilatura で本文を取得する **Pattern H 構造同型** で実装する
(FierceBiotech / The Register と同じ流れ)。

per-source 設計 (実 API 応答ベース):

- 毎サイクル直近 ``HN_SLIDING_WINDOW_SECONDS`` 秒以内に投稿された
  ``points > HN_MIN_POINTS`` のストーリーを全件取得する sliding window 設計
- increment 用の Redis state は持たず、dedup は下流の
  ``articles.source_url UNIQUE`` (Pattern R) / ``pending_html_articles.url
  UNIQUE`` (Pattern H) の ``ON CONFLICT DO NOTHING`` に委ねる
- ``url=None`` の hit (Ask HN / Show HN テキスト投稿等) は yield せずに skip
- ``DAILY_REQUEST_LIMIT`` は持たない (HN は cron 1 回/日、Algolia API 無料)
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
    """Hacker News 用 ``XxxSource`` (Algolia Search API, Pattern H)。

    business critical drop (``url=None`` skip / 空 title skip) は collect 内で
    旧 Fetcher と同位置・同順序で実施する。``points>20`` の閾値は Algolia の
    server-side numericFilters で既に drop 済のため collect では行わない。
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
                continue  # Ask HN / text-only post: external URL を持たない
            title = (hit.get("title") or "")[:_TITLE_MAX_LENGTH]
            if not title:
                continue  # business: title 欠落 skip
            published = _parse_created_at(hit.get("created_at"))
            yield FetchedArticle(
                title=title,
                url=raw_url,
                body=None,
                published_at=published.value if published is not None else None,
            )
