"""AssessmentService — Stage 4 のユースケース組み立てと永続化境界 (Pattern A')。

ドメイン層 (Draft / Entity / Ready) と AI 層 (``InScope`` / ``OutOfScope``) を結び、
判定実行 → 永続化 (楽観的ロック) → race 敗北時は読み戻し → Outcome 構築の順序を担う。

precondition (extraction 存在 + 未 in-scope 評価 + 未 out-of-scope 評価) は呼び出し側で
`ReadyForAssessment.try_advance_from` が gatekeeper として保証済 (spec §3.1)。
本 Service は precondition 分岐を持たない (`SkippedOutcome` / `AlreadyXxxOutcome`
は廃止、spec §2)。

`match response: case InScope() / case OutOfScope()` の tagged-union dispatch は
AI レスポンス境界 parse の正当な分岐として維持 (spec §1.3)。

楽観的ロック敗北 (broker 重複配信 / 並行 worker) は Repository.save が ``None`` を
返す。Service は勝者を `find_by_extraction_id` で読み戻し通常 Outcome を返す
(spec §4.6)。``AIProviderError`` は ACL boundary
(``map_provider_to_assessment``) で Stage 4 marker (``AssessmentRecoverableError`` /
``AssessmentTerminalSkipError``) に詰め替え、Task 層は Stage 4 marker のみで
3 marker dispatch を行う (PR6 wire-in)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.assessment.audit_repository import AssessmentAuditRepository
from app.analysis.assessment.domain.in_scope import (
    InScopeAssessment,
    InScopeAssessmentDraft,
)
from app.analysis.assessment.domain.out_of_scope import (
    OutOfScopeAssessment,
    OutOfScopeAssessmentDraft,
)
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import AssessmentCategoryMissingError
from app.analysis.assessment.out_of_scope_repository import OutOfScopeRepository
from app.analysis.assessment.provider_mapping import map_provider_to_assessment
from app.analysis.assessment.repository import InScopeRepository
from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.envelope import AssessmentCall
from app.analysis.classifier.schema import InScope, OutOfScope
from app.analysis.errors.provider import AIProviderError

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# AssessmentOutcome — Service 戻り値の tagged union (Pattern A' 後の縮退版)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InScopeOutcome:
    """in-scope として永続化された (新規 INSERT または race 敗北後の読み戻し)。"""

    assessment: InScopeAssessment


@dataclass(frozen=True, slots=True)
class OutOfScopeOutcome:
    """out-of-scope として永続化された (新規 INSERT または race 敗北後の読み戻し)。"""

    assessment: OutOfScopeAssessment


AssessmentOutcome = InScopeOutcome | OutOfScopeOutcome
"""Stage 4 の実行結果型。Task 層は `isinstance` で chain 判定する。"""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AssessmentService:
    """1 record の判定と永続化を行うアトミックなユースケース。

    Stage 4: Stage 3 で永続化された ``Extraction`` の `translated_title` /
    `summary` (Ready 経由で渡される) に対して判定を実行する。原文は読まない。
    Classifier の返却型により ``InScope`` / ``OutOfScope`` を型で受け取り、
    それぞれ ``InScopeAssessment`` / ``OutOfScopeAssessment`` ドメイン Entity に
    詰め替えて永続化する。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForAssessment,
        classifier: BaseClassifier,
    ) -> AssessmentOutcome:
        """Ready 型を受け取り判定 → 永続化 → Outcome を返す。

        precondition は型で保証済 (Ready を受けた時点で extraction 存在 +
        未 in-scope 評価 + 未 out-of-scope 評価)。AI 呼び出し中は session を保持しない
        (slow IO 中の DB 接続専有を避ける)。

        Raises:
            ``AnalysisDomainError`` のサブクラス (Task 層 retry に委ねる)。
        """
        # PR3: classifier 戻り値が AssessmentCall envelope 化。call.result が
        # tagged-union dispatch 軸。raw_response / raw_category / raw_topic /
        # prompt_version は audit 焼付 (PR6 で append_in_scope/out_of_scope に
        # envelope そのまま渡す) で参照する。
        try:
            call = await classifier.classify(
                title_ja=ready.translated_title,
                summary_ja=ready.summary,
            )
        except AIProviderError as exc:
            # ACL boundary: provider error を Stage 4 Layer 1 marker に wrap。
            # ``from exc`` で __cause__ に元 AIProvider*Error を紐付け、
            # ``recording.py::_extract_error_chain`` が wrapper marker → 元
            # provider error の 2 段以上を audit ``payload.error_chain`` に
            # 記録できるようにする。
            raise map_provider_to_assessment(exc) from exc

        response = call.result

        async with self._session_factory() as session:
            match response:
                case InScope():
                    return await self._handle_in_scope(
                        session,
                        ready=ready,
                        in_scope=response,
                        envelope=call,
                        model_name=classifier.model_name,
                    )
                case OutOfScope():
                    return await self._handle_out_of_scope(
                        session,
                        ready=ready,
                        out_of_scope=response,
                        envelope=call,
                        model_name=classifier.model_name,
                    )
                case _:
                    assert_never(response)

    async def _handle_in_scope(
        self,
        session: AsyncSession,
        *,
        ready: ReadyForAssessment,
        in_scope: InScope,
        envelope: AssessmentCall,
        model_name: str,
    ) -> InScopeOutcome:
        """InScope を Draft に詰め替えて永続化し、Outcome を返す。"""
        in_scope_repo = InScopeRepository(session)

        draft = InScopeAssessmentDraft.from_in_scope(
            in_scope,
            translated_title=ready.translated_title,
            summary=ready.summary,
        )
        category_id = await in_scope_repo.get_category_id_by_slug(
            in_scope.category.value
        )
        if category_id is None:
            # Layer 2-B 業務 invariant 違反: AI が catalog 未登録の slug を返した。
            # AssessmentTerminalSkipError 継承 → Task 層は audit
            # (code="assessment_category_missing") を焼いて即 return
            # (article / extraction は保持、catalog 拡張で復旧)。
            raise AssessmentCategoryMissingError(
                f"AI returned unknown category slug: {in_scope.category.value!r}"
            )

        saved = await in_scope_repo.save(
            draft,
            extraction_id=ready.extraction_id,
            category_id=category_id,
            ai_model=model_name,
        )

        if saved is not None:
            # 同 session で audit を焼く (業務 INSERT と同一 tx で原子的 commit)。
            # race lost (saved=None) の場合は actor SSoT 保持で audit を焼かない
            # (勝者 task が自身の audit を焼く、二重記録を避ける)。
            await AssessmentAuditRepository(session).append_in_scope(
                ready=ready,
                envelope=envelope,
                assessment=saved,
                ai_model=model_name,
                category_slug=in_scope.category.value,
                code="assessed_in_scope",
            )

        await session.commit()

        if saved is None:
            # 楽観的ロック敗北 (broker 重複配信 or 並行 worker) — 勝者を読み戻す
            # (spec §4.6)
            logger.info(
                "assessment_in_scope_concurrent_write",
                extraction_id=ready.extraction_id,
            )
            saved = await in_scope_repo.find_by_extraction_id(ready.extraction_id)
            if saved is None:
                # ON CONFLICT で race 敗北なのに行が無い = Pattern A' 違反 / DB 異常
                raise RuntimeError(
                    "assessment_in_scope_race_winner_missing: "
                    f"extraction_id={ready.extraction_id}"
                )

        logger.info(
            "assessment_in_scope_completed",
            assessment_id=saved.id,
            extraction_id=ready.extraction_id,
            category=in_scope.category.value,
            topic=draft.topic_name.root,
        )
        return InScopeOutcome(assessment=saved)

    async def _handle_out_of_scope(
        self,
        session: AsyncSession,
        *,
        ready: ReadyForAssessment,
        out_of_scope: OutOfScope,
        envelope: AssessmentCall,
        model_name: str,
    ) -> OutOfScopeOutcome:
        """OutOfScope を Draft に詰め替えて永続化し、Outcome を返す。"""
        out_of_scope_repo = OutOfScopeRepository(session)

        draft = OutOfScopeAssessmentDraft.from_out_of_scope(out_of_scope)
        saved = await out_of_scope_repo.save(
            draft,
            extraction_id=ready.extraction_id,
            ai_model=model_name,
        )

        if saved is not None:
            # 同 session で audit を焼く (in-scope 経路と対称)。
            # race lost で saved=None の場合は audit を焼かない (actor SSoT)。
            await AssessmentAuditRepository(session).append_out_of_scope(
                ready=ready,
                envelope=envelope,
                assessment=saved,
                ai_model=model_name,
                code="assessed_out_of_scope",
            )

        await session.commit()

        if saved is None:
            logger.info(
                "assessment_out_of_scope_concurrent_write",
                extraction_id=ready.extraction_id,
            )
            saved = await out_of_scope_repo.find_by_extraction_id(ready.extraction_id)
            if saved is None:
                raise RuntimeError(
                    "assessment_out_of_scope_race_winner_missing: "
                    f"extraction_id={ready.extraction_id}"
                )

        logger.info(
            "assessment_out_of_scope_completed",
            extraction_id=ready.extraction_id,
        )
        return OutOfScopeOutcome(assessment=saved)
