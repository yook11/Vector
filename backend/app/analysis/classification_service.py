"""Classification サービス — Stage 2 の処理組み立てと DB 永続化。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.schema import Classified, OutOfScope
from app.analysis.errors import ProviderError
from app.analysis.extraction.domain import Extraction
from app.analysis.extraction.repository import ExtractionRepository
from app.analysis.rejection_repository import RejectionRepository
from app.analysis.repository import AnalysisRepository
from app.models.article_analysis import ArticleAnalysis
from app.models.article_rejection import ArticleRejection
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ClassificationResult:
    """Stage 2 分類ユースケースの結果。

    ``classified`` / ``already_classified`` のみがチェーンを継続させる。
    ``rejected`` / ``already_rejected`` / ``skipped`` は embedding 生成に進まない。
    """

    status: Literal[
        "classified",
        "rejected",
        "already_classified",
        "already_rejected",
        "skipped",
    ]


class ClassificationService:
    """1 記事の分類と結果永続化を行うアトミックなユースケース。

    Stage 2: Stage 1 の構造化出力（DB から読み出し）に対して分類を実行する。
    原文は読まない。Classifier の返却型により Classified / OutOfScope を型で
    受け取り、それぞれ ArticleAnalysis / ArticleRejection に詰め替えて永続化する。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self, article_id: int, classifier: BaseClassifier
    ) -> ClassificationResult:
        """1 記事に対して分類を実行する。

        Returns:
            status を含む ClassificationResult。

        Raises:
            AnalysisDomainError のサブクラス。
        """
        async with self._session_factory() as session:
            extraction_repo = ExtractionRepository(session)
            analysis_repo = AnalysisRepository(session)
            rejection_repo = RejectionRepository(session)

            # extraction を取得（Stage 1 未完了なら skip）
            extraction = await extraction_repo.find_by_article_id(article_id)
            if extraction is None:
                logger.warning(
                    "classification_extraction_not_found", article_id=article_id
                )
                return ClassificationResult("skipped")

            # 冪等性チェック（既に分類済み／rejected 済みならスキップ）
            if await analysis_repo.find_by_extraction_id(extraction.id) is not None:
                return ClassificationResult("already_classified")
            if await rejection_repo.find_by_extraction_id(extraction.id) is not None:
                return ClassificationResult("already_rejected")

            # 既存トピック取得（プロンプトガイド用）
            existing_topics = await analysis_repo.get_existing_topics_by_category()

            # AI による分類（ドメイン tagged union で受け取る）
            response = await classifier.classify(
                title_ja=extraction.translated_title,
                summary_ja=extraction.summary,
                entities=list(extraction.entities),
                existing_topics_by_category=existing_topics,
            )

            match response:
                case Classified():
                    await self._persist_analysis(
                        session,
                        analysis_repo,
                        extraction=extraction,
                        classified=response,
                        model_name=classifier.model_name,
                    )
                    logger.info(
                        "classification_completed",
                        article_id=article_id,
                        extraction_id=extraction.id,
                        impact_level=response.impact_level,
                        category=response.category.value,
                        topic=response.topic.root,
                    )
                    return ClassificationResult("classified")

                case OutOfScope():
                    await self._persist_rejection(
                        session,
                        rejection_repo,
                        extraction_id=extraction.id,
                        out_of_scope=response,
                        model_name=classifier.model_name,
                    )
                    logger.info(
                        "classification_rejected",
                        article_id=article_id,
                        extraction_id=extraction.id,
                    )
                    return ClassificationResult("rejected")

                case _:
                    assert_never(response)

    async def _persist_analysis(
        self,
        session: AsyncSession,
        analysis_repo: AnalysisRepository,
        *,
        extraction: Extraction,
        classified: Classified,
        model_name: str,
    ) -> None:
        """Classified を ArticleAnalysis に詰め替えて永続化する。"""
        category_id = await analysis_repo.get_category_id_by_slug(
            classified.category.value
        )
        if category_id is None:
            raise ProviderError(
                f"AI returned unknown category slug: {classified.category.value!r}"
            )
        topic_id = await analysis_repo.find_or_create_topic(
            classified.topic.root, classified.topic_label_ja, category_id
        )
        analysis = ArticleAnalysis.from_classification(
            extraction_id=extraction.id,
            translated_title=extraction.translated_title,
            summary=extraction.summary,
            topic_id=topic_id,
            impact_level=classified.impact_level,
            reasoning=strip_html_tags(classified.reasoning) or "",
            model_name=model_name,
        )
        await analysis_repo.save_analysis(analysis)
        await session.commit()

    async def _persist_rejection(
        self,
        session: AsyncSession,
        rejection_repo: RejectionRepository,
        *,
        extraction_id: int,
        out_of_scope: OutOfScope,
        model_name: str,
    ) -> None:
        """OutOfScope を ArticleRejection に詰め替えて永続化する。"""
        rejection = ArticleRejection(
            extraction_id=extraction_id,
            reasoning=strip_html_tags(out_of_scope.reasoning) or "",
            ai_model=model_name,
        )
        await rejection_repo.save_rejection(rejection)
        await session.commit()
