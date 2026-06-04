"""AssessmentService — Stage 4 の AI 判定と永続化境界。

``ReadyForAssessment`` が precondition と audit 用参照値を保証するため、
Service は AI 呼び出し、結果別の保存、audit + commit だけを担う。楽観的ロックに
敗れた worker は audit / commit せず ``None`` を返し、下流 chain も起動しない。
"""

from __future__ import annotations

from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import map_provider_to_assessment
from app.analysis.assessment.repository import AssessmentRepository
from app.audit.stages.assessment import AssessmentAuditRepository
from app.logfire.article_stage import set_assessment_stage_result

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AssessmentService:
    """1 record の判定と永続化を行うアトミックなユースケース。

    Ready 経由の翻訳済み title / summary だけを判定し、原文は読まない。
    ``AssessmentCall`` は保存・監査へそのまま渡し、DB を下流の SSoT とする。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForAssessment,
        assessor: BaseAssessor,
    ) -> int | None:
        """Ready 型を受け取り判定 → 永続化 → 下流 chain 用 id を返す。

        AI 呼び出し中は session を保持しない。楽観的ロック敗北時は ``None`` を返し、
        勝者だけが audit と commit を行う。

        Returns:
            in-scope 成功時: 永続化された ``in_scope_assessments`` 行の id
                (Task 層が ``EmbeddingTrigger`` に詰めて kiq に流すキー)
            out-of-scope 成功時: ``None`` (Stage 5 chain なし)
            楽観ロック敗北時: ``None`` (Task 層は下流 chain を起動しない、
                勝者が crash 等で chain に失敗した case の救済は reconcile cron
                経路に委ねる)

        Raises:
            ``AssessmentRecoverableError`` / ``AssessmentTerminalError``
            (Task 層 retry に委ねる)。
        """
        try:
            call = await assessor.assess(
                title_ja=ready.translated_title,
                summary_ja=ready.summary,
            )
        except AIProviderError as exc:
            # Stage marker に詰め替え、audit で元 provider error まで辿れるよう
            # ``__cause__`` を保持する。
            raise map_provider_to_assessment(exc) from exc

        curation_id = ready.curation_id

        async with self._session_factory() as session:
            match call:
                case AssessmentCall(result=InScope()):
                    # `call` は ``AssessmentCall[InScope]`` に narrow される
                    assessment_id = await AssessmentRepository(session).save_in_scope(
                        call, ready=ready
                    )
                    # 楽観的ロック敗北時は、勝者だけが audit / commit する。
                    if assessment_id is None:
                        logger.info(
                            "assessment_in_scope_concurrent_write",
                            curation_id=curation_id,
                        )
                        set_assessment_stage_result("skipped")
                        return None
                    # 業務 INSERT + audit を同一 tx で commit
                    await AssessmentAuditRepository(session).append_in_scope(
                        ready=ready,
                        call=call,
                    )
                    await session.commit()
                    logger.info(
                        "assessment_in_scope_completed",
                        curation_id=curation_id,
                    )
                    set_assessment_stage_result("in_scope")
                    return assessment_id

                case AssessmentCall(result=OutOfScope()):
                    # `call` は ``AssessmentCall[OutOfScope]`` に narrow される
                    assessment_id = await AssessmentRepository(
                        session
                    ).save_out_of_scope(call, ready=ready)
                    # 楽観的ロック敗北時は、勝者だけが audit / commit する。
                    if assessment_id is None:
                        logger.info(
                            "assessment_out_of_scope_concurrent_write",
                            curation_id=curation_id,
                        )
                        set_assessment_stage_result("skipped")
                        return None
                    # 業務 INSERT + audit を同一 tx で commit
                    await AssessmentAuditRepository(session).append_out_of_scope(
                        ready=ready,
                        call=call,
                    )
                    await session.commit()
                    logger.info(
                        "assessment_out_of_scope_completed",
                        curation_id=curation_id,
                    )
                    set_assessment_stage_result("out_of_scope")
                    # Stage 5 chain なし
                    return None

                case _:
                    assert_never(call)
