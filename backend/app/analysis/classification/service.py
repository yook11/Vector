"""ClassificationService — Stage 2 のユースケース組み立てと永続化境界。

ドメイン層 (Draft / Entity) と AI 層 (``Classified`` / ``OutOfScope``) を結び、
冪等性チェック → 分類実行 → Topic 解決 → 永続化 → commit の順序を担う。
冪等分岐 (already_classified / already_rejected / skipped) では commit を呼ばない。
``ProviderError`` (unknown category 等) は Service で捕まえず Task 層 retry に委ねる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.classification.domain.analysis import Analysis, AnalysisDraft
from app.analysis.classification.domain.rejection import Rejection, RejectionDraft
from app.analysis.classification.rejection_repository import RejectionRepository
from app.analysis.classification.repository import AnalysisRepository
from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.schema import Classified, OutOfScope
from app.analysis.errors import ProviderError
from app.analysis.extraction.repository import ExtractionRepository

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# ClassificationOutcome — Service 戻り値の tagged union
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassifiedOutcome:
    """新規に Classified として永続化された。"""

    analysis: Analysis


@dataclass(frozen=True, slots=True)
class RejectedOutcome:
    """新規に OutOfScope として永続化された。"""

    rejection: Rejection


@dataclass(frozen=True, slots=True)
class AlreadyClassifiedOutcome:
    """同 extraction が既に Classified 済み (冪等ヒット)。"""

    analysis: Analysis


@dataclass(frozen=True, slots=True)
class AlreadyRejectedOutcome:
    """同 extraction が既に OutOfScope 済み (冪等ヒット)。"""

    rejection: Rejection


@dataclass(frozen=True, slots=True)
class SkippedOutcome:
    """Stage 1 (extraction) が未完了で Stage 2 を実行できなかった。"""

    reason: Literal["extraction_not_found"]


ClassificationOutcome = (
    ClassifiedOutcome
    | RejectedOutcome
    | AlreadyClassifiedOutcome
    | AlreadyRejectedOutcome
    | SkippedOutcome
)
"""Stage 2 の実行結果型。Task 層は ``isinstance`` で chain するか判断する。"""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ClassificationService:
    """1 記事の分類と結果永続化を行うアトミックなユースケース。

    Stage 2: Stage 1 の構造化出力 (DB から読み出し) に対して分類を実行する。
    原文は読まない。Classifier の返却型により ``Classified`` / ``OutOfScope``
    を型で受け取り、それぞれ ``Analysis`` / ``Rejection`` ドメイン Entity に
    詰め替えて永続化する。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self, article_id: int, classifier: BaseClassifier
    ) -> ClassificationOutcome:
        """1 記事に対して分類を実行する。

        Raises:
            ``AnalysisDomainError`` のサブクラス (Task 層 retry に委ねる)。
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
                logger.info(
                    "classification_skipped_extraction_not_found",
                    article_id=article_id,
                )
                return SkippedOutcome(reason="extraction_not_found")

            # 冪等性チェック（既に分類済み／rejected 済みならスキップ）
            existing_analysis = await analysis_repo.find_by_extraction_id(extraction.id)
            if existing_analysis is not None:
                logger.info(
                    "classification_already_classified",
                    article_id=article_id,
                    extraction_id=extraction.id,
                )
                return AlreadyClassifiedOutcome(analysis=existing_analysis)

            existing_rejection = await rejection_repo.find_by_extraction_id(
                extraction.id
            )
            if existing_rejection is not None:
                logger.info(
                    "classification_already_rejected",
                    article_id=article_id,
                    extraction_id=extraction.id,
                )
                return AlreadyRejectedOutcome(rejection=existing_rejection)

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
                    return await self._handle_classified(
                        session,
                        analysis_repo,
                        article_id=article_id,
                        extraction_id=extraction.id,
                        translated_title=extraction.translated_title,
                        summary=extraction.summary,
                        classified=response,
                        model_name=classifier.model_name,
                    )
                case OutOfScope():
                    return await self._handle_out_of_scope(
                        session,
                        rejection_repo,
                        article_id=article_id,
                        extraction_id=extraction.id,
                        out_of_scope=response,
                        model_name=classifier.model_name,
                    )
                case _:
                    assert_never(response)

    async def _handle_classified(
        self,
        session: AsyncSession,
        analysis_repo: AnalysisRepository,
        *,
        article_id: int,
        extraction_id: int,
        translated_title: str,
        summary: str,
        classified: Classified,
        model_name: str,
    ) -> ClassifiedOutcome:
        """Classified を Draft に詰め替えて永続化し、Entity を返す。"""
        draft = AnalysisDraft.from_classified(
            classified,
            translated_title=translated_title,
            summary=summary,
        )

        category_id = await analysis_repo.get_category_id_by_slug(
            classified.category.value
        )
        if category_id is None:
            raise ProviderError(
                f"AI returned unknown category slug: {classified.category.value!r}"
            )

        topic_id = await analysis_repo.find_or_create_topic(
            draft.topic_name, draft.topic_label_ja, category_id
        )
        persisted = await analysis_repo.save(
            draft,
            extraction_id=extraction_id,
            topic_id=topic_id,
            ai_model=model_name,
        )
        await session.commit()

        logger.info(
            "classification_completed",
            article_id=article_id,
            extraction_id=extraction_id,
            impact_level=draft.impact_level,
            category=classified.category.value,
            topic=draft.topic_name.root,
        )

        analysis = Analysis.from_draft(
            draft,
            id=persisted.id,
            extraction_id=extraction_id,
            topic_id=topic_id,
            ai_model=model_name,
            analyzed_at=persisted.analyzed_at,
        )
        return ClassifiedOutcome(analysis=analysis)

    async def _handle_out_of_scope(
        self,
        session: AsyncSession,
        rejection_repo: RejectionRepository,
        *,
        article_id: int,
        extraction_id: int,
        out_of_scope: OutOfScope,
        model_name: str,
    ) -> RejectedOutcome:
        """OutOfScope を Draft に詰め替えて永続化し、Entity を返す。"""
        draft = RejectionDraft.from_out_of_scope(out_of_scope)
        persisted = await rejection_repo.save(
            draft,
            extraction_id=extraction_id,
            ai_model=model_name,
        )
        await session.commit()

        logger.info(
            "classification_rejected",
            article_id=article_id,
            extraction_id=extraction_id,
        )

        rejection = Rejection.from_draft(
            draft,
            id=persisted.id,
            extraction_id=extraction_id,
            ai_model=model_name,
            rejected_at=persisted.rejected_at,
        )
        return RejectedOutcome(rejection=rejection)
