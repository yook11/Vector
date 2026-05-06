"""Hacker News 用 Fetcher — Pattern H 設計 (Algolia HN Search API)。

collection-acquisition-redesign Phase 1e。HN はソース仕様が API ベース (Algolia
HN Search API) で RSS / Atom feed を持たないが、API hit の ``url`` は外部の
任意サイトを指すため本文は HN 側で取得できない。よって ``ReadyForArticle``
invariant (body ≥ 50 chars) を API 単独で満たせず、``PendingHtmlFetch`` を
yield し後段 ``extract_html_body`` task が trafilatura で本文を取得する
**Pattern H 構造同型** で実装する (FierceBiotech / The Register と同じ流れ)。

per-source 設計 (実 API 応答ベース):

- 毎サイクル直近 ``HN_SLIDING_WINDOW_SECONDS`` 秒以内に投稿された
  ``points > HN_MIN_POINTS`` のストーリーを全件取得する sliding window 設計
- increment 用の Redis state は持たず、dedup は下流の
  ``ArticleUrlRepository.upsert_returning`` 側 ``ON CONFLICT DO NOTHING``
  に委ねる
- ``url=None`` の hit (Ask HN / Show HN テキスト投稿等) は yield せずに skip
- ``language`` はリンク先が任意の外部サイトのため feed-level で確定できず
  ``None`` 直書き (PROVIDES からも外す)
- ``DAILY_REQUEST_LIMIT`` は持たない (HN は cron 1 回/日、Algolia API 無料)
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar

import httpx
import structlog

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FailureReason,
    FetchedEntry,
    FetchOutcome,
    PendingHtmlFetch,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

# HN フェッチャー固有の運用値。Settings (環境変数経由) には載せない。
# 動的に切り替える運用要件が出た時点でコンストラクタ DI に昇格させる。
HN_MIN_POINTS = 20
HN_HITS_PER_PAGE = 100
HN_SLIDING_WINDOW_SECONDS = 86400  # 24h

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_TITLE_MAX_LENGTH = 500
_AUTHOR_MAX_LENGTH = 200


def _parse_created_at(raw: str | None) -> PublishedAt | None:
    """Algolia の ``created_at`` (ISO 8601 + ``Z``) を UTC ``PublishedAt`` に変換。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


class HackerNewsFetcher:
    """Hacker News 用 Pattern H Fetcher (Algolia Search API)。

    PROVIDES に列挙したフィールドは API 仕様で 100% 提供される前提:

    - ``guid``: ``hit["objectID"]`` (Algolia 主キー、グローバル一意)
    - ``site_name``: hardcode "Hacker News"

    ``language`` はリンク先が任意の外部サイトのため PROVIDES に **含めない**
    (RSS Pattern H ソースとの差異)。``author`` は probabilistic のため
    metadata に詰めるが PROVIDES には含めない。``tags`` / ``image_url`` は
    HN 側で提供されないため ``()`` / ``None`` を直書きする。
    """

    NAME: ClassVar[str] = "Hacker News"
    ENDPOINT_URL: ClassVar[str] = "https://hn.algolia.com/api/v1/search_by_date"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        hits = await self._fetch_recent_hits()
        for hit in hits:
            outcome = self._convert_hit(hit, source_id)
            if outcome is not None:
                yield outcome

    async def _fetch_recent_hits(self) -> list[dict[str, Any]]:
        """Algolia HN Search API から sliding window 内のストーリーを取得する。

        Raises:
            PermanentFetchError: 403 / 404 / 410 / 451 / SSRF host 拒否。
            TemporaryFetchError: 429 / 5xx / タイムアウト / DNS 一時失敗。
        """
        since = int(time.time()) - HN_SLIDING_WINDOW_SECONDS
        params: dict[str, str | int] = {
            "tags": "story",
            "hitsPerPage": HN_HITS_PER_PAGE,
            "numericFilters": f"points>{HN_MIN_POINTS},created_at_i>{since}",
        }

        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(
                    self.ENDPOINT_URL,
                    params=params,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {self.NAME}") from e
                raise TemporaryFetchError(f"HTTP {status}: {self.NAME}") from e
            except httpx.RequestError as e:
                raise TemporaryFetchError(f"request error: {self.NAME}: {e}") from e
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e

            data = response.json()

        hits: list[dict[str, Any]] = list(data.get("hits", []))
        if not hits:
            logger.info("hn_no_new_stories", source=self.NAME)
        return hits

    def _convert_hit(
        self,
        hit: dict[str, Any],
        source_id: int,
    ) -> FetchOutcome | None:
        """1 hit を ``FetchOutcome`` に変換する純関数。

        ``url`` 欠落 (Ask HN / テキスト投稿等) は ``None`` を返して skip する
        (``Failed`` ではない: HN 側で外部 URL を持たない投稿は採取対象外)。
        """
        raw_url = hit.get("url")
        if not isinstance(raw_url, str) or not raw_url:
            return None

        title = (hit.get("title") or "")[:_TITLE_MAX_LENGTH]
        if not title:
            return Failed(
                reason=FailureReason(
                    code="title_missing",
                    retryable=False,
                    detail="hn_title_missing",
                )
            )

        try:
            source_url = SafeUrl(raw_url)
        except ValueError:
            return Failed(
                reason=FailureReason(
                    code="extraction_empty",
                    retryable=False,
                    detail=f"invalid_link:{raw_url[:100]}",
                )
            )

        published_at_hint = _parse_created_at(hit.get("created_at"))

        raw_author = hit.get("author")
        if isinstance(raw_author, str) and raw_author:
            author = raw_author[:_AUTHOR_MAX_LENGTH] or None
        else:
            author = None

        guid_raw = hit.get("objectID")
        guid = guid_raw if isinstance(guid_raw, str) and guid_raw else None

        metadata: dict[str, Any] = {
            "site_name": self.NAME,
        }
        if author:
            metadata["author"] = author
        if guid:
            metadata["guid"] = guid

        return FetchedEntry(
            item=PendingHtmlFetch(
                title=title,
                source_id=source_id,
                source_url=source_url,
                published_at_hint=published_at_hint,
            ),
            metadata=metadata,
        )
