"""Analysis サービス — 処理の組み立てと DB 永続化を担う。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.analyzer.base import BaseAnalyzer
from app.analysis.errors import InvalidInputError, ProviderError
from app.analysis.repository import AnalysisRepository
from app.models.article_analysis import ArticleAnalysis
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AnalysisResult:
    """記事分析ユースケースの結果。"""

    status: Literal["created", "already_exists", "skipped"]
    analysis_id: int | None = None


class ArticleAnalysisService:
    """1 記事の分析と結果永続化を行うアトミックなユースケース。

    セッションの管理はサービス内部で完結し、呼び出し側は session factory のみ渡す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, article_id: int, analyzer: BaseAnalyzer) -> AnalysisResult:
        """1 記事に対して分析を実行する。

        Returns:
            status と必要に応じた analysis_id を含む AnalysisResult。

        Raises:
            AnalysisDomainError のサブクラス（InvalidInputError を除く）。
            リトライ判断は呼び出し側の責務。
        """
        async with self._session_factory() as session:
            repo = AnalysisRepository(session)

            # 冪等性チェック
            existing = await repo.find_by_article_id(article_id)
            if existing is not None:
                return AnalysisResult("already_exists", analysis_id=existing.id)

            # 記事を取得
            article = await repo.get_article(article_id)
            if article is None:
                logger.warning("analysis_article_not_found", article_id=article_id)
                return AnalysisResult("skipped")

            # 既存トピックを取得（プロンプトのガイド用）
            existing_topics = await repo.get_existing_topics_by_category()

            # AI による分析
            try:
                data = await analyzer.analyze(
                    title=article.original_title,
                    description=article.original_description,
                    content=article.original_content,
                    existing_topics_by_category=existing_topics,
                )
            except InvalidInputError:
                await repo.mark_article_skipped(article)
                await session.commit()
                logger.warning(
                    "analysis_invalid_input",
                    article_id=article_id,
                )
                return AnalysisResult("skipped")

            # カテゴリ ID を取得
            category_id = await repo.get_category_id_by_slug(data.category_slug)
            if category_id is None:
                raise ProviderError(
                    f"AI returned unknown category slug: {data.category_slug!r}"
                )

            # Topic の find-or-create
            topic_id = await repo.find_or_create_topic(data.topic_name, category_id)

            # サニタイズと永続化
            analysis = ArticleAnalysis(
                news_article_id=article.id,
                translated_title=strip_html_tags(data.title) or "",
                summary=strip_html_tags(data.summary) or "",
                impact_level=data.impact_level,
                reasoning=strip_html_tags(data.reasoning) or "",
                ai_model=analyzer.model_name,
                topic_id=topic_id,
            )
            await repo.save_analysis(analysis)
            await session.commit()

            logger.info(
                "analysis_completed",
                article_id=article_id,
                impact_level=data.impact_level,
                category=data.category_slug,
                topic=data.topic_name,
            )
            return AnalysisResult("created", analysis_id=analysis.id)


async def mark_article_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    article_id: int,
) -> None:
    """記事を恒久的にスキップ対象としてマークする（Task の最終試行時に使用）。"""
    async with session_factory() as session:
        repo = AnalysisRepository(session)
        article = await repo.get_article(article_id)
        if article is not None:
            await repo.mark_article_skipped(article)
            await session.commit()
