"""AssessmentService — Stage 4 のユースケース組み立てと永続化境界 (Pattern A')。

ドメイン層 (Entity / Ready) と AI 層 (``InScope`` / ``OutOfScope``) を結び、
判定実行 → 永続化 (楽観的ロック) → 勝者は audit + commit / 敗者は短絡の順序を担う。

precondition (extraction 存在 + 未 in-scope 評価 + 未 out-of-scope 評価) は呼び出し側で
`ReadyForAssessment.try_advance_from` が gatekeeper として保証済 (spec §3.1)。
本 Service は precondition 分岐を持たない。

`match response: case InScope() / case OutOfScope()` の tagged-union dispatch は
AI レスポンス境界 parse の正当な分岐として維持 (spec §1.3)。各 case で
永続化先 Repository / audit 焼き先を切り替え、post-save の順序
(audit → commit → log) は両 arm で同一に保つ。

楽観的ロック敗北 (broker 重複配信 / 並行 worker) は Repository.save が ``None`` を
返す。敗者経路は ``None`` を返して短絡し、audit を焼かず commit も呼ばない
(actor SSoT — 勝者 task が自身の audit を焼く、二重記録回避)。Task 層
は ``None`` を観測したら Stage 5 chain を起動しない。勝者 task が crash 等で
chain に失敗した case の救済は本 Service の責務外で、別経路の reconcile cron が
担う。``AIProviderError`` は ACL boundary (``map_provider_to_assessment``) で
Stage 4 marker (``AssessmentRecoverableError`` / ``AssessmentTerminalSkipError``)
に詰め替え、Task 層は Stage 4 marker のみで 3 marker dispatch を行う。

戻り値は ``InScopeAssessment | OutOfScopeAssessment | None``。``None`` は race
敗北 (勝者が存在し、本 task は短絡すべき) を意味する。Task 層は ``None`` 早期
return → ``isinstance(result, InScopeAssessment)`` で Stage 5 chain を判定する。
"""

from __future__ import annotations

from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.schema import InScope, OutOfScope
from app.analysis.assessment.audit_repository import AssessmentAuditRepository
from app.analysis.assessment.domain.in_scope import InScopeAssessment
from app.analysis.assessment.domain.out_of_scope import OutOfScopeAssessment
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.out_of_scope_repository import OutOfScopeRepository
from app.analysis.assessment.provider_mapping import map_provider_to_assessment
from app.analysis.assessment.repository import InScopeRepository
from app.analysis.errors.provider import AIProviderError

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AssessmentService:
    """1 record の判定と永続化を行うアトミックなユースケース。

    Stage 4: Stage 3 で永続化された ``Extraction`` の `translated_title` /
    `summary` (Ready 経由で渡される) に対して判定を実行する。原文は読まない。
    Assessor の返却型により ``InScope`` / ``OutOfScope`` を型で受け取り、
    それぞれ ``InScopeAssessment`` / ``OutOfScopeAssessment`` ドメイン Entity に
    詰め替えて永続化する。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForAssessment,
        assessor: BaseAssessor,
    ) -> InScopeAssessment | OutOfScopeAssessment | None:
        """Ready 型を受け取り判定 → 永続化 → Entity を返す (敗者は ``None``)。

        precondition は型で保証済 (Ready を受けた時点で extraction 存在 +
        未 in-scope 評価 + 未 out-of-scope 評価)。AI 呼び出し中は session を保持しない
        (slow IO 中の DB 接続専有を避ける)。

        楽観ロック敗北時は ``None`` を返す。Task 層は ``None`` を観測したら下流
        chain を起動しない。勝者が crash 等で chain に失敗した case の救済は
        reconcile cron 経路に委ねる (本 Service の責務外)。

        Raises:
            ``AnalysisDomainError`` のサブクラス (Task 層 retry に委ねる)。
        """
        # PR3: assessor 戻り値が AssessmentCall envelope 化。call.result が
        # tagged-union dispatch 軸。raw_response / raw_category / raw_topic /
        # prompt_version は audit 焼付 (append_in_scope/out_of_scope に envelope
        # そのまま渡す) で参照する。
        try:
            call = await assessor.assess(
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
        extraction_id = ready.extraction_id

        async with self._session_factory() as session:
            match response:
                case InScope():
                    in_scope_saved = await InScopeRepository(session).save(
                        response,
                        ready=ready,
                        ai_model=assessor.model_name,
                    )
                    # race lost — audit / commit は不要 (勝者 task が audit を焼く、
                    # 二重記録回避)。reconcile cron に救済を委譲。
                    if in_scope_saved is None:
                        logger.info(
                            "assessment_in_scope_concurrent_write",
                            extraction_id=extraction_id,
                        )
                        return None
                    # winner — 業務 INSERT + audit を同一 tx で commit
                    await AssessmentAuditRepository(session).append_in_scope(
                        ready=ready,
                        envelope=call,
                        assessment=in_scope_saved,
                        in_scope=response,
                        ai_model=assessor.model_name,
                    )
                    await session.commit()
                    logger.info(
                        "assessment_in_scope_completed",
                        extraction_id=extraction_id,
                    )
                    return in_scope_saved

                case OutOfScope():
                    out_of_scope_saved = await OutOfScopeRepository(session).save(
                        response,
                        ready=ready,
                        ai_model=assessor.model_name,
                    )
                    # race lost — audit / commit は不要 (勝者 task が audit を焼く、
                    # 二重記録回避)。reconcile cron に救済を委譲。
                    if out_of_scope_saved is None:
                        logger.info(
                            "assessment_out_of_scope_concurrent_write",
                            extraction_id=extraction_id,
                        )
                        return None
                    # winner — 業務 INSERT + audit を同一 tx で commit
                    await AssessmentAuditRepository(session).append_out_of_scope(
                        ready=ready,
                        envelope=call,
                        assessment=out_of_scope_saved,
                        ai_model=assessor.model_name,
                    )
                    await session.commit()
                    logger.info(
                        "assessment_out_of_scope_completed",
                        extraction_id=extraction_id,
                    )
                    return out_of_scope_saved

                case _:
                    assert_never(response)
