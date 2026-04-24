"""Extraction サービス — Stage 1 の処理組み立てと DB 永続化。"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.errors import InvalidInputError
from app.analysis.extraction.domain import Extraction
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.repository import ExtractionRepository

logger = structlog.get_logger(__name__)


class ExtractionService:
    """1 記事の事実抽出と結果永続化を行うアトミックなユースケース。

    Stage 1: 原文を読み、翻訳タイトル・事実ベース要約・エンティティを抽出する。
    分類（カテゴリ・トピック・インパクト）は Stage 2 の責務。

    戻り値は ``Extraction | None``:
    - ``Extraction``: 新規抽出 or 冪等ヒット (どちらも後続 Stage 2 に chain)
    - ``None``: 記事欠落 or 入力不正 (chain しない)
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self, article_id: int, extractor: BaseExtractor
    ) -> Extraction | None:
        """1 記事に対して事実抽出を実行する。

        Raises:
            AnalysisDomainError のサブクラス（InvalidInputError を除く）。
        """
        async with self._session_factory() as session:
            repo = ExtractionRepository(session)

            # 冪等性チェック
            existing = await repo.find_by_article_id(article_id)
            if existing is not None:
                return existing

            # 記事を取得
            article = await repo.get_article(article_id)
            if article is None:
                logger.warning("extraction_article_not_found", article_id=article_id)
                return None

            # AI による抽出
            try:
                result = await extractor.extract(
                    title=article.original_title,
                    content=article.original_content,
                )
            except InvalidInputError:
                logger.warning(
                    "extraction_invalid_input",
                    article_id=article_id,
                )
                return None

            # 永続化 (Repository は identity のみ返す)
            persisted = await repo.save(
                result,
                article_id=article.id,
                ai_model=extractor.model_name,
            )
            await session.commit()

            # 永続化結果と分析結果を組み合わせて Entity を組み立てる
            extraction = Extraction.from_result(
                result,
                id=persisted.id,
                ai_model=extractor.model_name,
                extracted_at=persisted.extracted_at,
            )

            logger.info(
                "extraction_completed",
                article_id=article_id,
                extraction_id=extraction.id,
                entity_count=len(result.entities),
            )
            return extraction
