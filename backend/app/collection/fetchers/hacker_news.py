"""Hacker News 用 Fetcher — Pattern H 設計 (Algolia HN Search API)。

collection-acquisition-redesign Phase 1e。HN はソース仕様が API ベース (Algolia
HN Search API) で RSS / Atom feed を持たないが、API hit の ``url`` は外部の
任意サイトを指すため本文は HN 側で取得できない。よって ``AnalyzableArticle``
invariant (body ≥ 50 chars) を API 単独で満たせず、``ObservedArticle`` を
yield し後段 ``extract_html_body`` task が trafilatura で本文を取得する
**Pattern H 構造同型** で実装する (FierceBiotech / The Register と同じ流れ)。

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

import httpx
import structlog

from app.collection.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.algolia_hn_client import HackerNewsApiClient
from app.collection.fetchers.tools.fetched_article import FetchedArticle

logger = structlog.get_logger(__name__)

# HN フェッチャー固有の運用値。Settings (環境変数経由) には載せない。
# 動的に切り替える運用要件が出た時点でコンストラクタ DI に昇格させる。
HN_MIN_POINTS = 20
HN_HITS_PER_PAGE = 100
HN_SLIDING_WINDOW_SECONDS = 86400  # 24h

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
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


class HackerNewsAdapter:
    """Hacker News 用 ``SourceAdapter`` (Algolia Search API, Pattern H)。

    旧 ``HackerNewsFetcher`` と同じ Algolia HN Search API 経路を踏襲しつつ、
    HTTP 取得 + numericFilters 構築は ``HackerNewsApiClient`` に委譲する。
    business critical drop (``url=None`` skip / 空 title skip) は本 Adapter
    内で旧 Fetcher と同位置・同順序で実施する (``hacker_news.py:131-137``
    に対応)。``points>20`` の閾値は Algolia の server-side numericFilters で
    既に drop 済のため Adapter では行わない (旧仕様維持)。
    """

    def __init__(
        self,
        *,
        source_name: str,
        client: HackerNewsApiClient | None = None,
    ) -> None:
        self._source_name = source_name
        self._client = client or HackerNewsApiClient()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        hits = await self._client.search_recent_stories(
            source_name=self._source_name,
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
                continue  # business: title 欠落 skip (旧 hacker_news.py:135-137)
            published = _parse_created_at(hit.get("created_at"))
            yield FetchedArticle(
                title=title,
                url=raw_url,
                body=None,
                published_at=published.value if published is not None else None,
            )
