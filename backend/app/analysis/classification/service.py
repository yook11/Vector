"""ClassificationService — Stage D のユースケース組み立てと永続化境界 (Pattern A')。

ドメイン層 (Draft / Entity / Ready) と AI 層 (``Classified`` / ``OutOfScope``) を結び、
分類実行 → 永続化 (楽観的ロック) → race 敗北時は読み戻し → Outcome 構築の順序を担う。

precondition (extraction 存在 + 未分類 + 未却下) は呼び出し側で
`ReadyForClassification.try_advance_from` が gatekeeper として保証済 (spec §3.1)。
本 Service は precondition 分岐を持たない (`SkippedOutcome` / `AlreadyXxxOutcome`
は廃止、spec §2)。

`match response: case Classified() / case OutOfScope()` の tagged-union dispatch は
AI レスポンス境界 parse の正当な分岐として維持 (spec §1.3)。

楽観的ロック敗北 (broker 重複配信 / 並行 worker) は Repository.save が ``None`` を
返す。Service は勝者を `find_by_extraction_id` で読み戻し通常 Outcome を返す
(spec §4.6)。`ProviderError` (unknown category 等) は Service で捕まえず Task 層
retry に委ねる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.classification.domain.analysis import Analysis, AnalysisDraft
from app.analysis.classification.domain.ready import ReadyForClassification
from app.analysis.classification.domain.rejection import Rejection, RejectionDraft
from app.analysis.classification.rejection_repository import RejectionRepository
from app.analysis.classification.repository import AnalysisRepository
from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.schema import Classified, OutOfScope
from app.analysis.errors import ProviderError

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# ClassificationOutcome — Service 戻り値の tagged union (Pattern A' 後の縮退版)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassifiedOutcome:
    """Classified として永続化された (新規 INSERT または race 敗北後の読み戻し)。"""

    analysis: Analysis


@dataclass(frozen=True, slots=True)
class RejectedOutcome:
    """OutOfScope として永続化された (新規 INSERT または race 敗北後の読み戻し)。"""

    rejection: Rejection


ClassificationOutcome = ClassifiedOutcome | RejectedOutcome
"""Stage D の実行結果型。Task 層は `isinstance` で chain 判定する。"""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ClassificationService:
    """1 record の分類と永続化を行うアトミックなユースケース。

    Stage D: Stage C で永続化された ``Extraction`` の `translated_title` /
    `summary` (Ready 経由で渡される) に対して分類を実行する。原文は読まない。
    Classifier の返却型により ``Classified`` / ``OutOfScope`` を型で受け取り、
    それぞれ ``Analysis`` / ``Rejection`` ドメイン Entity に詰め替えて永続化する。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForClassification,
        classifier: BaseClassifier,
    ) -> ClassificationOutcome:
        """Ready 型を受け取り分類 → 永続化 → Outcome を返す。

        precondition は型で保証済 (Ready を受けた時点で extraction 存在 +
        未分類 + 未却下)。AI 呼び出し中は session を保持しない (slow IO 中の
        DB 接続専有を避ける)。

        Raises:
            ``AnalysisDomainError`` のサブクラス (Task 層 retry に委ねる)。
        """
        response = await classifier.classify(
            title_ja=ready.translated_title,
            summary_ja=ready.summary,
        )

        async with self._session_factory() as session:
            match response:
                case Classified():
                    return await self._handle_classified(
                        session,
                        ready=ready,
                        classified=response,
                        model_name=classifier.model_name,
                    )
                case OutOfScope():
                    return await self._handle_out_of_scope(
                        session,
                        ready=ready,
                        out_of_scope=response,
                        model_name=classifier.model_name,
                    )
                case _:
                    assert_never(response)

    async def _handle_classified(
        self,
        session: AsyncSession,
        *,
        ready: ReadyForClassification,
        classified: Classified,
        model_name: str,
    ) -> ClassifiedOutcome:
        """Classified を Draft に詰め替えて永続化し、Outcome を返す。"""
        analysis_repo = AnalysisRepository(session)

        draft = AnalysisDraft.from_classified(
            classified,
            translated_title=ready.translated_title,
            summary=ready.summary,
        )
        category_id = await analysis_repo.get_category_id_by_slug(
            classified.category.value
        )
        if category_id is None:
            raise ProviderError(
                f"AI returned unknown category slug: {classified.category.value!r}"
            )

        saved = await analysis_repo.save(
            draft,
            extraction_id=ready.extraction_id,
            category_id=category_id,
            ai_model=model_name,
        )
        await session.commit()

        if saved is None:
            # 楽観的ロック敗北 (broker 重複配信 or 並行 worker) — 勝者を読み戻す
            # (spec §4.6)
            logger.info(
                "classification_concurrent_write",
                extraction_id=ready.extraction_id,
                article_id=ready.article_id,
            )
            saved = await analysis_repo.find_by_extraction_id(ready.extraction_id)
            if saved is None:
                # ON CONFLICT で race 敗北なのに行が無い = Pattern A' 違反 / DB 異常
                raise RuntimeError(
                    "classification_race_winner_missing: "
                    f"extraction_id={ready.extraction_id}"
                )

        logger.info(
            "classification_completed",
            article_id=ready.article_id,
            extraction_id=ready.extraction_id,
            category=classified.category.value,
            topic=draft.topic_name.root,
        )
        return ClassifiedOutcome(analysis=saved)

    async def _handle_out_of_scope(
        self,
        session: AsyncSession,
        *,
        ready: ReadyForClassification,
        out_of_scope: OutOfScope,
        model_name: str,
    ) -> RejectedOutcome:
        """OutOfScope を Draft に詰め替えて永続化し、Outcome を返す。"""
        rejection_repo = RejectionRepository(session)

        draft = RejectionDraft.from_out_of_scope(out_of_scope)
        saved = await rejection_repo.save(
            draft,
            extraction_id=ready.extraction_id,
            ai_model=model_name,
        )
        await session.commit()

        if saved is None:
            logger.info(
                "rejection_concurrent_write",
                extraction_id=ready.extraction_id,
                article_id=ready.article_id,
            )
            saved = await rejection_repo.find_by_extraction_id(ready.extraction_id)
            if saved is None:
                raise RuntimeError(
                    "rejection_race_winner_missing: "
                    f"extraction_id={ready.extraction_id}"
                )

        logger.info(
            "classification_rejected",
            article_id=ready.article_id,
            extraction_id=ready.extraction_id,
        )
        return RejectedOutcome(rejection=saved)
