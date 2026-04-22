"""Extraction サービス — Stage 1 の処理組み立てと DB 永続化。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.errors import InvalidInputError
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.repository import ExtractionRepository
from app.models.article_extraction import ArticleExtraction

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ExtractionResult:
    """Stage 1 抽出ユースケースの結果。"""

    status: Literal["created", "already_exists", "skipped"]
    extraction_id: int | None = None


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
            status と必要に応じた extraction_id を含む ExtractionResult。

        Raises:
            AnalysisDomainError のサブクラス（InvalidInputError を除く）。
        """
        async with self._session_factory() as session:
            repo = ExtractionRepository(session)

            # 冪等性チェック
            if await repo.find_by_article_id(article_id) is not None:
                return ExtractionResult("already_exists")

            # 記事を取得
            article = await repo.get_article(article_id)
            if article is None:
                logger.warning("extraction_article_not_found", article_id=article_id)
                return ExtractionResult("skipped")

            # AI による抽出
            try:
                data = await extractor.extract(
                    title=article.original_title,
                    content=article.original_content,
                )
            except InvalidInputError:
                logger.warning(
                    "extraction_invalid_input",
                    article_id=article_id,
                )
                return ExtractionResult("skipped")

            # ArticleExtraction 作成（cascade で entities も永続化）
            extraction = ArticleExtraction.from_extraction_response(
                article_id=article.id,
                response=data,
                model_name=extractor.model_name,
            )
            await repo.save_extraction(extraction)
            await session.commit()

            logger.info(
                "extraction_completed",
                article_id=article_id,
                extraction_id=extraction.id,
                entity_count=len(data.entities),
            )
            return ExtractionResult("created", extraction_id=extraction.id)
