"""AssessmentService — Stage 4 の AI 判定と永続化境界。

``ReadyForAssessment`` が precondition を、明示引数 ``analyzable_article_id`` が
監査主語を保証するため、Service は AI 呼び出し、結果別の保存、audit + commit だけを
担う。重複保存を見送った場合は audit / commit せず ``ALREADY_ASSESSED`` を返す。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_providers.errors import AIProviderError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import to_assessment_error
from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.assessment.metrics import record_assessment_processing_outcome
from app.analysis.assessment.repository import AssessmentRepository
from app.audit.stages.assessment import AssessmentAuditRepository
from app.logfire.article_stage import set_assessment_stage_result
from app.models.outbox_event import OutboxEvent

logger = structlog.get_logger(__name__)


class AssessmentCompletionKind(StrEnum):
    """保存または処理済み確認による正常終了の種類。"""

    IN_SCOPE = "in_scope"
    OUT_OF_SCOPE = "out_of_scope"
    ALREADY_ASSESSED = "already_assessed"


@dataclass(frozen=True, slots=True)
class AssessmentCompletion:
    """対象内の新規保存時だけ保存済み記事IDを伴う正常終了。"""

    kind: AssessmentCompletionKind
    analyzed_article_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AssessmentCompletionKind):
            raise TypeError("kind must be an AssessmentCompletionKind")
        if self.kind is AssessmentCompletionKind.IN_SCOPE:
            if (
                type(self.analyzed_article_id) is not int
                or self.analyzed_article_id <= 0
            ):
                raise ValueError("in_scope requires a positive integer article ID")
        elif self.analyzed_article_id is not None:
            raise ValueError("only in_scope may carry an article ID")


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
        *,
        analyzable_article_id: int,
    ) -> AssessmentCompletion:
        """判定結果のcommitまたは重複保存の見送りを正常終了として返す。"""
        try:
            call = await assessor.assess(
                title_ja=ready.translated_title,
                summary_ja=ready.summary,
            )
        except AIProviderError as exc:
            # 元のプロバイダー例外を原因チェーンにも保持する。
            raise to_assessment_error(exc) from exc

        curation_id = ready.curation_id

        async with self._session_factory() as session:
            match call:
                case AssessmentCall(result=InScope()):
                    # `call` は ``AssessmentCall[InScope]`` に narrow される
                    article = InScopeAnalyzedArticle.from_ready_and_assessment_result(
                        ready=ready,
                        assessment_result=call.result,
                    )
                    analyzed_article_id = await AssessmentRepository(
                        session
                    ).save_in_scope(article)
                    # 楽観的ロック敗北時は、勝者だけが audit / commit する。
                    if analyzed_article_id is None:
                        logger.info(
                            "assessment_in_scope_concurrent_write",
                            curation_id=curation_id,
                        )
                        set_assessment_stage_result("skipped")
                        return AssessmentCompletion(
                            AssessmentCompletionKind.ALREADY_ASSESSED
                        )
                    # 結果・audit・Outboxを同一トランザクションで確定する。
                    await AssessmentAuditRepository(session).append_in_scope(
                        ready=ready,
                        call=call,
                        article_id=analyzable_article_id,
                    )
                    event = ArticleAssessedInScope(
                        curation_id=curation_id,
                        analyzed_article_id=analyzed_article_id,
                    )
                    session.add(
                        OutboxEvent(
                            event_type=ArticleAssessedInScope.EVENT_TYPE,
                            schema_version=ArticleAssessedInScope.SCHEMA_VERSION,
                            payload=event.model_dump(mode="json"),
                        )
                    )
                    await session.commit()
                    logger.info(
                        "assessment_in_scope_completed",
                        curation_id=curation_id,
                    )
                    set_assessment_stage_result("in_scope")
                    record_assessment_processing_outcome("in_scope")
                    return AssessmentCompletion(
                        AssessmentCompletionKind.IN_SCOPE, analyzed_article_id
                    )

                case AssessmentCall(result=OutOfScope()):
                    # `call` は ``AssessmentCall[OutOfScope]`` に narrow される
                    out_of_scope_article_id = await AssessmentRepository(
                        session
                    ).save_out_of_scope(call, ready=ready)
                    # 楽観的ロック敗北時は、勝者だけが audit / commit する。
                    if out_of_scope_article_id is None:
                        logger.info(
                            "assessment_out_of_scope_concurrent_write",
                            curation_id=curation_id,
                        )
                        set_assessment_stage_result("skipped")
                        return AssessmentCompletion(
                            AssessmentCompletionKind.ALREADY_ASSESSED
                        )
                    # 業務 INSERT + audit を同一 tx で commit
                    await AssessmentAuditRepository(session).append_out_of_scope(
                        ready=ready,
                        call=call,
                        article_id=analyzable_article_id,
                    )
                    await session.commit()
                    logger.info(
                        "assessment_out_of_scope_completed",
                        curation_id=curation_id,
                    )
                    set_assessment_stage_result("out_of_scope")
                    record_assessment_processing_outcome("out_of_scope")
                    # Stage 5 chain なし
                    return AssessmentCompletion(AssessmentCompletionKind.OUT_OF_SCOPE)

                case _:
                    assert_never(call)
