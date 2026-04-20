"""RSS 基底フェッチャー — Template Method パターンによる共通フロー。

条件付き GET（ETag / Last-Modified）→ feedparser → convert_entry → persist
の共通フローを保持し、エントリ変換ロジックをサブクラスでオーバーライド可能にする。
"""

import asyncio
from calendar import timegm
from datetime import UTC, datetime

import feedparser
import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.fetchers.http_cache import get_http_cache, set_http_cache
from app.collection.ingestion.persister import (
    ArticleCandidate,
    SourceFetchResult,
    persist_new_articles,
    to_safe_url,
)
from app.models.news_source import NewsSource
from app.utils.sanitize import strip_html_tags

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


# --- ユーティリティ関数（サブクラスからも利用可能） ---


def parse_published_date(entry: dict) -> datetime | None:
    """feedparser エントリから公開日時を抽出する。"""
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct is None:
        return None
    try:
        timestamp = timegm(time_struct)
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def extract_guid(entry: dict) -> str | None:
    """feedparser エントリから一意な識別子を抽出する。

    entry.id（feedparser が <guid> をマップしたもの）を優先し、
    無い場合は entry.link にフォールバックする。
    """
    guid = entry.get("id") or entry.get("guid")
    if guid:
        return guid[:2048]
    link = entry.get("link")
    if link:
        return link[:2048]
    return None


def extract_full_content(entry: dict) -> str | None:
    """RSS エントリに含まれる全文コンテンツを抽出する（存在すれば）。

    feedparser は content:encoded を entry.content に正規化する。
    """
    content_list = entry.get("content")
    if content_list and isinstance(content_list, list):
        for c in content_list:
            value = c.get("value", "")
            if value:
                return value
    return None


class BaseRssFetcher:
    """RSS フェッチャーの基底クラス。

    共通フロー（条件付き GET → feedparser → convert_entry → persist）を保持し、
    サブクラスは convert_entry をオーバーライドしてソース固有の変換ロジックを実装する。
    """

    def convert_entry(self, entry: dict) -> ArticleCandidate | None:
        """feedparser エントリを ArticleCandidate に変換する。

        デフォルト実装は汎用的な変換を行う。
        ソース固有のロジックが必要な場合はオーバーライドする。
        """
        raw_url = entry.get("link", "") or extract_guid(entry) or ""
        if not raw_url:
            return None

        safe_url = to_safe_url(raw_url)
        if safe_url is None:
            return None

        return ArticleCandidate(
            url=safe_url,
            title=strip_html_tags(entry.get("title", ""))[:500],
            description=strip_html_tags(
                entry.get("summary") or entry.get("description")
            ),
            published_at=parse_published_date(entry),
        )

    async def fetch(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        source: NewsSource,
    ) -> SourceFetchResult:
        """RSS ソース 1 件を取得・処理する。

        Raises:
            PermanentFetchError: 403 / 404 / 410 / 451、フィードのパース失敗。
            TemporaryFetchError: 429 / 5xx / タイムアウト / ネットワークエラー。
        """
        # 条件付き GET 用のヘッダを Redis から読み出す
        cached_etag, cached_last_modified = await get_http_cache(source.id)

        headers: dict[str, str] = {}
        if cached_etag:
            headers["If-None-Match"] = cached_etag
        if cached_last_modified:
            headers["If-Modified-Since"] = cached_last_modified

        try:
            response = await client.get(
                str(source.endpoint_url), headers=headers, timeout=HTTP_TIMEOUT
            )

            # 304 Not Modified — 新着なし（正常系として空結果を返す）
            if response.status_code == 304:
                logger.info("feed_not_modified", source=source.name)
                return SourceFetchResult()

            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.error("feed_http_error", source=source.name, status=status)
            if status in (403, 404, 410, 451):
                raise PermanentFetchError(f"HTTP {status}: {source.name}") from e
            raise TemporaryFetchError(f"HTTP {status}: {source.name}") from e
        except httpx.RequestError as e:
            logger.error("feed_request_error", source=source.name, error=str(e))
            raise TemporaryFetchError(f"request error: {source.name}: {e}") from e

        # 次回のフェッチサイクルのため Redis に永続化
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        await set_http_cache(source.id, etag, last_modified)

        # フィードをパース
        feed = await asyncio.to_thread(feedparser.parse, response.text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "feed_parse_error",
                source=source.name,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {source.name}: {feed.bozo_exception}"
            )

        # エントリを ArticleCandidate に変換
        candidates: list[ArticleCandidate] = []
        for entry in feed.entries:
            candidate = self.convert_entry(entry)
            if candidate is not None:
                candidates.append(candidate)

        if not candidates:
            return SourceFetchResult()

        return await persist_new_articles(session, source, candidates)
