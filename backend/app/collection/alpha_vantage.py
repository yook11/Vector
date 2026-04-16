"""Alpha Vantage News Sentiment API クライアント。"""

from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.news_fetcher import SourceFetchResult
from app.collection.source_helpers import get_last_successful_fetch_at
from app.config import settings
from app.models.fetch_log import FetchLog
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.utils.sanitize import is_safe_url, strip_html_tags

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


class AlphaVantageClient:
    """Alpha Vantage News Sentiment API クライアント。"""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self.http_client = http_client
        self.base_url = settings.av_api_base_url
        self.api_key = settings.av_api_key.get_secret_value()

    async def _check_daily_quota(
        self, source: NewsSource, session: AsyncSession
    ) -> bool:
        """日次クォータを超過していなければ True を返す。"""
        today_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        stmt = (
            select(sa_func.count())
            .select_from(FetchLog)
            .where(
                FetchLog.source_id == source.id,
                FetchLog.fetched_at >= today_start,
            )
        )
        result = await session.execute(stmt)
        count = result.scalar_one()
        return count < settings.av_max_daily_requests

    async def fetch_and_save_articles(
        self,
        source: NewsSource,
        session: AsyncSession,
    ) -> SourceFetchResult:
        """Alpha Vantage のニュース記事を取得し news_articles に保存する。

        - URL の一括突合で重複排除する（RSS/HN と同パターン）
        - 新規/スキップ件数を含む SourceFetchResult を返す
        """
        result = SourceFetchResult(source_id=source.id)

        if not self.api_key:
            logger.info("av_skipped_no_api_key", source=source.name)
            return result

        # 日次クォータをチェック
        if not await self._check_daily_quota(source, session):
            logger.warning("av_daily_quota_exceeded", source=source.name)
            result.success = False
            result.error_message = "Daily API quota exceeded"
            return result

        # fetch_logs から直近フェッチ時刻を導出
        last_fetched = await get_last_successful_fetch_at(session, source.id)

        params: dict[str, str | int] = {
            "function": "NEWS_SENTIMENT",
            "topics": settings.av_topics,
            "sort": "LATEST",
            "limit": settings.av_limit,
            "apikey": self.api_key,
        }
        if last_fetched:
            params["time_from"] = last_fetched.strftime("%Y%m%dT%H%M")

        try:
            response = await self.http_client.get(
                self.base_url, params=params, timeout=HTTP_TIMEOUT
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

        # 一括重複排除用に URL を組み立てる
        articles_data: list[tuple[dict, str]] = []
        for item in feed:
            url = item.get("url", "")
            if not url:
                continue
            articles_data.append((item, url))

        if not articles_data:
            return result

        urls = [u for _, u in articles_data]
        existing_urls: set[str] = set()

        chunk_size = 500
        for i in range(0, len(urls), chunk_size):
            chunk = urls[i : i + chunk_size]
            stmt = select(NewsArticle.original_url).where(
                NewsArticle.original_url.in_(chunk)
            )
            rows = await session.execute(stmt)
            # TODO: SafeUrl の __eq__ が str と互換になれば str() 不要
            existing_urls.update(str(row[0]) for row in rows.all())

        # 新規記事を作成
        max_new = settings.max_articles_per_fetch
        new_count = 0

        for item, url in articles_data:
            if url in existing_urls:
                result.skipped_count += 1
                continue

            # --- XSS対策: URLスキーム検証 ---
            # Alpha Vantage APIから取得したURLも外部データであり、信頼できない。
            # javascript: 等の危険なスキームをDB保存前に排除する。
            if not is_safe_url(url):
                logger.warning(
                    "unsafe_url_skipped",
                    source=source.name,
                    url=url[:200],
                )
                result.skipped_count += 1
                continue

            if new_count >= max_new:
                logger.info("av_fetch_limit_reached", source=source.name, max=max_new)
                break

            try:
                published_at = _parse_av_time(item["time_published"])
            except (ValueError, KeyError):
                published_at = None

            article = NewsArticle(
                original_title=strip_html_tags(item.get("title", ""))[:500],
                original_description=strip_html_tags(item.get("summary")),
                original_url=url,
                news_source_id=source.id,
                published_at=published_at,
            )

            session.add(article)
            result.new_articles.append(article)
            new_count += 1
            existing_urls.add(url)

        result.new_count = new_count
        logger.info(
            "av_fetch_completed",
            source=source.name,
            new=new_count,
            skipped=result.skipped_count,
        )
        return result
