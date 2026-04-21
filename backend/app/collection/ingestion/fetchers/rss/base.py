"""RSS 基底フェッチャー — Template Method パターンによる共通フロー。

条件付き GET（ETag / Last-Modified）→ feedparser → convert_entry → persist
の共通フローを保持し、エントリ変換ロジックをサブクラスでオーバーライド可能にする。
"""

import asyncio

import feedparser
import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.fetchers.http_cache import get_http_cache, set_http_cache
from app.collection.ingestion.persister import (
    ArticleCandidate,
    PersistResult,
    persist_new_articles,
)
from app.domain.safe_url import SafeUrl
from app.models.news_source import NewsSource

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


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
        return ArticleCandidate.from_external(
            raw_url=raw_url, raw_title=entry.get("title", "")
        )

    async def fetch(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        source: NewsSource,
    ) -> PersistResult:
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
                return PersistResult()

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
        # dict 組み立てにより URL 重複は先勝ちで型レベル排除される
        candidates: dict[SafeUrl, ArticleCandidate] = {}
        for entry in feed.entries:
            candidate = self.convert_entry(entry)
            if candidate is not None:
                candidates.setdefault(candidate.url, candidate)

        if not candidates:
            return PersistResult()

        return await persist_new_articles(session, source, candidates)
