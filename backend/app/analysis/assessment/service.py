"""AssessmentService — Stage 4 のユースケース組み立てと永続化境界 (案 3 適用)。

ドメイン (Ready) と AI 層 (``AssessmentCall[InScope]`` / ``AssessmentCall[OutOfScope]``)
を結び、判定実行 → 永続化 (楽観的ロック) → 勝者は audit + commit / 敗者は短絡の
順序を担う。

precondition (extraction 存在 + 未 in-scope 評価 + 未 out-of-scope 評価) +
audit に必要な参照値 (``article_id``) は呼び出し側 (Stage 4 Task) で
`ReadyForAssessment.try_advance_from` が gatekeeper として構造保証済
(案 3 = 厚い Ready + 下流 Stage 自身が処理開始時に構築)。
本 Service は precondition 分岐 / 逆引きを持たない。

``match call: case AssessmentCall(result=InScope() | OutOfScope()):`` の dispatch
は Generic envelope に対する型 narrowing をそのまま使う。各 case で永続化先
Repository method を切り替え、post-save の順序 (audit → commit → log) は両 arm で
同一に保つ。Stage 4 で起きた事実は ``call`` envelope に閉じているため、Service /
Repository / AuditRepository は ``call`` 1 つを取り回す
(``ai_model`` 等の追加引数を caller が引き回さない、
`feedback_bc_boundary_guarantees_downstream`)。

楽観的ロック敗北 (broker 重複配信 / 並行 worker) は Repository.save が ``None`` を
返す。敗者経路は ``None`` を返して短絡し、audit を焼かず commit も呼ばない
(actor SSoT — 勝者 task が自身の audit を焼く、二重記録回避)。Task 層
は ``None`` を観測したら Stage 5 chain を起動しない。勝者 task が crash 等で
chain に失敗した case の救済は本 Service の責務外で、別経路の reconcile cron が
担う。``AIProviderError`` は ACL boundary (``map_provider_to_assessment``) で
Stage 4 marker (``AssessmentRecoverableError`` / ``AssessmentTerminalError``)
に詰め替え、Task 層は Stage 4 marker のみで 3 marker dispatch を行う。

戻り値は ``int | None`` (in-scope 成功時のみ assessment id、out-of-scope と
race 敗北は ``None``)。Task 層は ``None`` 早期 return で Stage 5 chain を抑止し、
非 ``None`` の場合は受け取った id で ``EmbeddingTrigger`` を構築して chain する
(Ready 構築は下流 Stage 5 task が処理開始時に行う、案 3)。
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

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AssessmentService:
    """1 record の判定と永続化を行うアトミックなユースケース。

    Stage 4: Stage 3 で永続化された ``Extraction`` の `translated_title` /
    `summary` (Ready 経由で渡される) に対して判定を実行する。原文は読まない。
    Assessor の返却型により ``AssessmentCall[InScope]`` /
    ``AssessmentCall[OutOfScope]`` を Generic envelope として受け取り、
    対応する Repository method (``save_in_scope`` / ``save_out_of_scope``) に
    ``call`` をそのまま渡して永続化する。Domain Entity は介在せず、
    永続化が確定したら DB を SSoT として下流が信用する
    (`feedback_bc_boundary_guarantees_downstream`)。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForAssessment,
        assessor: BaseAssessor,
    ) -> int | None:
        """Ready 型を受け取り判定 → 永続化 → 下流 chain 用 id を返す。

        precondition は型で保証済 (Ready を受けた時点で extraction 存在 +
        未 in-scope 評価 + 未 out-of-scope 評価)。AI 呼び出し中は session を保持しない
        (slow IO 中の DB 接続専有を避ける)。

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
            # ACL boundary: provider error を Stage 4 Layer 1 marker に wrap。
            # ``from exc`` で __cause__ に元 AIProvider*Error を紐付け、
            # ``error_chain.py::extract_error_chain`` が wrapper marker → 元
            # provider error の 2 段以上を audit ``payload.error_chain`` に
            # 記録できるようにする。
            raise map_provider_to_assessment(exc) from exc

        curation_id = ready.curation_id

        async with self._session_factory() as session:
            match call:
                case AssessmentCall(result=InScope()):
                    # `call` は ``AssessmentCall[InScope]`` に narrow される
                    assessment_id = await AssessmentRepository(session).save_in_scope(
                        call, ready=ready
                    )
                    # race lost — audit / commit は不要 (勝者 task が audit を焼く、
                    # 二重記録回避)。reconcile cron に救済を委譲。
                    if assessment_id is None:
                        logger.info(
                            "assessment_in_scope_concurrent_write",
                            curation_id=curation_id,
                        )
                        return None
                    # winner — 業務 INSERT + audit を同一 tx で commit
                    await AssessmentAuditRepository(session).append_in_scope(
                        ready=ready,
                        call=call,
                    )
                    await session.commit()
                    logger.info(
                        "assessment_in_scope_completed",
                        curation_id=curation_id,
                    )
                    return assessment_id

                case AssessmentCall(result=OutOfScope()):
                    # `call` は ``AssessmentCall[OutOfScope]`` に narrow される
                    assessment_id = await AssessmentRepository(
                        session
                    ).save_out_of_scope(call, ready=ready)
                    # race lost — audit / commit は不要 (勝者 task が audit を焼く、
                    # 二重記録回避)。reconcile cron に救済を委譲。
                    if assessment_id is None:
                        logger.info(
                            "assessment_out_of_scope_concurrent_write",
                            curation_id=curation_id,
                        )
                        return None
                    # winner — 業務 INSERT + audit を同一 tx で commit
                    await AssessmentAuditRepository(session).append_out_of_scope(
                        ready=ready,
                        call=call,
                    )
                    await session.commit()
                    logger.info(
                        "assessment_out_of_scope_completed",
                        curation_id=curation_id,
                    )
                    # Stage 5 chain なし
                    return None

                case _:
                    assert_never(call)
