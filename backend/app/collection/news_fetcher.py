"""ニュースフェッチャサービス — 登録済みニュースソースから記事を取得する。

オーケストレータ: ソース種別に応じたフェッチャーへの振り分け、
FetchLog の記録、結果の集計を行う。
"""

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fetch_log import FetchLog, FetchStatus
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource, SourceType

logger = structlog.get_logger(__name__)


@dataclass
class SourceFetchResult:
    """単一ソースのフェッチ結果。"""

    source_id: int
    success: bool = True
    new_count: int = 0
    skipped_count: int = 0
    error_message: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    new_articles: list[NewsArticle] = field(default_factory=list)


@dataclass
class FetchResult:
    """全ソース横断の集計結果。"""

    new_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    source_results: list[SourceFetchResult] = field(default_factory=list)
    new_article_ids: list[int] = field(default_factory=list)
    content_ready_ids: list[int] = field(default_factory=list)


async def fetch_news_for_sources(
    session: AsyncSession,
    sources: list[NewsSource],
) -> FetchResult:
    """指定したソースからニュース記事を取得する。

    Args:
        session: DB セッション。
        sources: 取得対象のアクティブな NewsSource のリスト。

    Returns:
        件数・エラー・ソース別結果を含む FetchResult。
    """
    result = FetchResult()

    if not sources:
        logger.info("fetch_skipped", reason="no sources provided")
        return result

    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
        }
    ) as client:
        for source in sources:
            logger.info(
                "fetching_source",
                source=source.name,
                type=source.source_type,
            )

            start_time = time.monotonic()

            if source.source_type == SourceType.RSS:
                from app.collection.rss_fetcher import fetch_rss_source

                source_result = await fetch_rss_source(client, session, source)
            elif source.source_type == SourceType.API:
                # endpoint_url のドメインに応じて API クライアントを振り分ける
                # TODO: スキーマ層を SafeUrl 対応にした後、str() 変換を削除
                domain = urlparse(str(source.endpoint_url)).hostname or ""
                if "algolia.com" in domain:
                    from app.collection.hacker_news import HackerNewsClient

                    hn_client = HackerNewsClient(client)
                    source_result = await hn_client.fetch_and_save_stories(
                        source=source, session=session
                    )
                elif "alphavantage.co" in domain:
                    from app.collection.alpha_vantage import AlphaVantageClient

                    av_client = AlphaVantageClient(client)
                    source_result = await av_client.fetch_and_save_articles(
                        source=source, session=session
                    )
                else:
                    logger.warning(
                        "unsupported_api_endpoint",
                        source=source.name,
                        endpoint_url=str(source.endpoint_url),
                    )
                    msg = f"Unsupported API endpoint: {source.endpoint_url}"
                    source_result = SourceFetchResult(
                        source_id=source.id,
                        success=False,
                        error_message=msg,
                    )
            else:
                logger.warning(
                    "unsupported_source_type",
                    source=source.name,
                    type=source.source_type,
                )
                source_result = SourceFetchResult(
                    source_id=source.id,
                    success=False,
                    error_message=f"Unsupported source type: {source.source_type}",
                )

            duration_ms = int((time.monotonic() - start_time) * 1000)

            # フェッチログを記録
            fetch_log = FetchLog(
                source_id=source.id,
                status=(
                    FetchStatus.SUCCESS if source_result.success else FetchStatus.ERROR
                ),
                articles_count=source_result.new_count,
                error_message=source_result.error_message,
                duration_ms=duration_ms,
            )
            session.add(fetch_log)

            result.source_results.append(source_result)
            result.new_count += source_result.new_count
            result.skipped_count += source_result.skipped_count

            if not source_result.success:
                result.error_count += 1
                if source_result.error_message:
                    result.errors.append(
                        f"{source.name}: {source_result.error_message}"
                    )

    await session.commit()

    # コミット後に ID を取り出す（呼び出し側は expire_on_commit=False 前提）
    for sr in result.source_results:
        for article in sr.new_articles:
            result.new_article_ids.append(article.id)
            if (
                article.original_content is not None
                and article.published_at is not None
            ):
                result.content_ready_ids.append(article.id)

    logger.info(
        "fetch_completed",
        sources=len(sources),
        new=result.new_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
