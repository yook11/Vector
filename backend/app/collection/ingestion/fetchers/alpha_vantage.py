"""Alpha Vantage News Sentiment API フェッチャー。"""

from datetime import UTC, datetime
from typing import ClassVar

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.ingestion.fetchers.source_helpers import (
    get_last_successful_fetch_at,
)
from app.collection.ingestion.persister import (
    ArticleCandidate,
    SourceFetchResult,
    persist_new_articles,
    to_safe_url,
)
from app.config import settings
from app.models.news_source import NewsSource
from app.utils.sanitize import strip_html_tags

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


def _parse_av_time(time_str: str) -> datetime:
    """Alpha Vantage の time_published 文字列を UTC datetime に変換する。

    標準形式: YYYYMMDDTHHMMSS（15 文字、秒あり）。
    フォールバック: YYYYMMDDTHHMM（13 文字、秒なし）。
    """
    # strptime は %M/%S を 1 桁でも受け付けてしまうため、長さで判別する
    t_pos = time_str.find("T")
    time_part = time_str[t_pos + 1 :] if t_pos >= 0 else ""
    if len(time_part) >= 6:
        return datetime.strptime(time_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    return datetime.strptime(time_str, "%Y%m%dT%H%M").replace(tzinfo=UTC)


class AlphaVantageFetcher:
    """Alpha Vantage News Sentiment API フェッチャー。"""

    DAILY_REQUEST_LIMIT: ClassVar[int | None] = 25

    async def fetch(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        source: NewsSource,
    ) -> SourceFetchResult:
        """Alpha Vantage のニュース記事を取得し ArticleCandidate 経由で永続化する。"""
        result = SourceFetchResult(source_id=source.id)

        api_key = settings.av_api_key.get_secret_value()
        if not api_key:
            logger.info("av_skipped_no_api_key", source=source.name)
            return result

        # fetch_logs から直近フェッチ時刻を導出
        last_fetched = await get_last_successful_fetch_at(session, source.id)

        params: dict[str, str | int] = {
            "function": "NEWS_SENTIMENT",
            "topics": settings.av_topics,
            "sort": "LATEST",
            "limit": settings.av_limit,
            "apikey": api_key,
        }
        if last_fetched:
            params["time_from"] = last_fetched.strftime("%Y%m%dT%H%M")

        try:
            response = await client.get(
                settings.av_api_base_url, params=params, timeout=HTTP_TIMEOUT
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(
                "av_http_error",
                source=source.name,
                status=e.response.status_code,
            )
            result.success = False
            result.error_message = f"HTTP {e.response.status_code}"
            return result
        except httpx.RequestError as e:
            logger.error("av_request_error", source=source.name, error=str(e))
            result.success = False
            result.error_message = str(e)
            return result

        data = response.json()

        # AV はエラー時も HTTP 200 + {"Information": "..."} を返す
        if "Information" in data:
            logger.error("av_api_error", source=source.name, info=data["Information"])
            result.success = False
            result.error_message = data["Information"][:500]
            return result

        feed = data.get("feed", [])
        if not feed:
            logger.info("av_no_articles", source=source.name)
            return result

        # フィードアイテムを ArticleCandidate に変換
        candidates: list[ArticleCandidate] = []
        for item in feed:
            url = item.get("url", "")
            if not url:
                continue

            safe_url = to_safe_url(url)
            if safe_url is None:
                logger.warning(
                    "unsafe_url_skipped",
                    source=source.name,
                    url=url[:200],
                )
                result.skipped_count += 1
                continue

            try:
                published_at = _parse_av_time(item["time_published"])
            except (ValueError, KeyError):
                published_at = None

            candidates.append(
                ArticleCandidate(
                    url=safe_url,
                    title=strip_html_tags(item.get("title", ""))[:500],
                    description=strip_html_tags(item.get("summary")),
                    published_at=published_at,
                )
            )

        if not candidates:
            return result

        # 永続化を共通ロジックに委譲
        persist_result = await persist_new_articles(session, source, candidates)
        result.new_count = persist_result.new_count
        result.skipped_count += persist_result.skipped_count
        result.new_discovered = persist_result.new_discovered

        logger.info(
            "av_fetch_completed",
            source=source.name,
            new=result.new_count,
            skipped=result.skipped_count,
        )
        return result
