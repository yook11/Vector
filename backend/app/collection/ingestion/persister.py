"""記事永続化 — フェッチャーが生成した ArticleCandidate を DB に保存する。

全フェッチャー（RSS / HN / AV）で共通の永続化ロジック:
URL 重複排除、max_articles 制限、session.add を一箇所に集約する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.domain.safe_url import SafeUrl
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ArticleCandidate:
    """フェッチャーが生成する記事の中間表現。

    ソース固有のデータ構造から変換された、永続化前の正規化済みデータ。
    """

    url: SafeUrl
    title: str
    description: str | None = None
    content: str | None = None
    published_at: datetime | None = None


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


def to_safe_url(raw: str) -> SafeUrl | None:
    """生文字列を SafeUrl に変換する。不正な URL は None を返す。"""
    try:
        return SafeUrl(raw)
    except (ValueError, ValidationError):
        return None


async def persist_new_articles(
    session: AsyncSession,
    source: NewsSource,
    candidates: list[ArticleCandidate],
) -> SourceFetchResult:
    """ArticleCandidate リストを重複排除して DB に保存する。

    Args:
        session: DB セッション（コミットは呼び出し側の責任）。
        source: 記事の取得元ソース。
        candidates: フェッチャーが生成した記事候補リスト。

    Returns:
        新規/スキップ件数と新規記事を含む SourceFetchResult。
    """
    result = SourceFetchResult(source_id=source.id)

    if not candidates:
        return result

    # 一括重複排除: 既存 URL を確認
    urls = [c.url for c in candidates]
    existing_urls: set[SafeUrl] = set()
    chunk_size = 500
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i : i + chunk_size]
        stmt = select(NewsArticle.original_url).where(
            NewsArticle.original_url.in_(chunk)
        )
        rows = await session.execute(stmt)
        existing_urls.update(row[0] for row in rows.all())

    # 新規記事を作成
    max_new = settings.max_articles_per_fetch
    new_count = 0

    for candidate in candidates:
        if candidate.url in existing_urls:
            result.skipped_count += 1
            continue

        if new_count >= max_new:
            logger.info("source_fetch_limit_reached", source=source.name, max=max_new)
            break

        article = NewsArticle(
            original_title=candidate.title,
            original_description=candidate.description,
            original_url=candidate.url,
            news_source_id=source.id,
            published_at=candidate.published_at,
        )

        if candidate.content:
            article.original_content = candidate.content[: settings.content_max_length]

        session.add(article)
        result.new_articles.append(article)
        new_count += 1
        # 同一バッチ内の後続候補で重複しないよう URL を記録
        existing_urls.add(candidate.url)

    result.new_count = new_count
    return result
