"""Extraction サービス — Stage 1 の処理組み立てと DB 永続化。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.errors import InvalidInputError
from app.analysis.extractor.base import BaseExtractor
from app.analysis.repository import AnalysisRepository
from app.models.article_analysis import ArticleAnalysis
from app.models.article_entity import ArticleEntity
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ExtractionResult:
    """Stage 1 抽出ユースケースの結果。"""

    status: Literal["created", "already_exists", "skipped"]
    analysis_id: int | None = None


class ExtractionService:
    """1 記事の事実抽出と結果永続化を行うアトミックなユースケース。

    Stage 1: 原文を読み、翻訳タイトル・事実ベース要約・エンティティを抽出する。
    分類（カテゴリ・トピック・インパクト）は Stage 2 の責務。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self, article_id: int, extractor: BaseExtractor
    ) -> ExtractionResult:
        """1 記事に対して事実抽出を実行する。

        Returns:
            status と必要に応じた analysis_id を含む ExtractionResult。

        Raises:
            AnalysisDomainError のサブクラス（InvalidInputError を除く）。
        """
        async with self._session_factory() as session:
            repo = AnalysisRepository(session)

            # 冪等性チェック
            existing = await repo.find_by_article_id(article_id)
            if existing is not None:
                return ExtractionResult("already_exists", analysis_id=existing.id)

            # 記事を取得
            article = await repo.get_article(article_id)
            if article is None:
                logger.warning("extraction_article_not_found", article_id=article_id)
                return ExtractionResult("skipped")

            # AI による抽出
            try:
                data = await extractor.extract(
                    title=article.original_title,
                    description=article.original_description,
                    content=article.original_content,
                )
            except InvalidInputError:
                await repo.mark_article_skipped(article)
                await session.commit()
                logger.warning(
                    "extraction_invalid_input",
                    article_id=article_id,
                )
                return ExtractionResult("skipped")

            # ArticleAnalysis 作成（Stage 2 の結果は未設定）
            analysis = ArticleAnalysis(
                news_article_id=article.id,
                translated_title=strip_html_tags(data.title_ja) or "",
                summary=strip_html_tags(data.summary_ja) or "",
                ai_model=extractor.model_name,
                # Stage 2 で設定される: topic_id, impact_level, reasoning
            )
            await repo.save_analysis(analysis)

            # エンティティ保存
            for entity_data in data.entities:
                entity = ArticleEntity(
                    article_analysis_id=analysis.id,
                    name=entity_data.name,
                    type=entity_data.type,
                )
                session.add(entity)
            await session.flush()

            await session.commit()

            logger.info(
                "extraction_completed",
                article_id=article_id,
                analysis_id=analysis.id,
                entity_count=len(data.entities),
            )
            return ExtractionResult("created", analysis_id=analysis.id)


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
