"""AssessmentService — Stage 4 のユースケース組み立てと永続化境界 (Pattern A')。

ドメイン層 (Entity / Ready) と AI 層 (``InScope`` / ``OutOfScope``) を結び、
判定実行 → 永続化 (楽観的ロック) → race 敗北時は読み戻し → Entity 返却の順序を担う。

precondition (extraction 存在 + 未 in-scope 評価 + 未 out-of-scope 評価) は呼び出し側で
`ReadyForAssessment.try_advance_from` が gatekeeper として保証済 (spec §3.1)。
本 Service は precondition 分岐を持たない。

`match response: case InScope() / case OutOfScope()` の tagged-union dispatch は
AI レスポンス境界 parse の正当な分岐として維持 (spec §1.3)。各 case で選ぶのは
「永続化先 Repository」「audit 焼き callable」「勝者読み戻し callable」「log code」
だけで、共通の post-save orchestration (audit + commit + race recovery + ログ) は
``_finalize`` が 1 本で担う。

楽観的ロック敗北 (broker 重複配信 / 並行 worker) は Repository.save が ``None`` を
返す。``_finalize`` は早期 return で勝者を `find_by_extraction_id` 経由で読み戻し
Entity を返す (spec §4.6)。``AIProviderError`` は ACL boundary
(``map_provider_to_assessment``) で Stage 4 marker (``AssessmentRecoverableError`` /
``AssessmentTerminalSkipError``) に詰め替え、Task 層は Stage 4 marker のみで
3 marker dispatch を行う (PR6 wire-in)。

戻り値は ``InScopeAssessment | OutOfScopeAssessment`` の Entity union。Task 層は
``isinstance(result, InScopeAssessment)`` で Stage 5 chain を判定する。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
    ) -> InScopeAssessment | OutOfScopeAssessment:
        """Ready 型を受け取り判定 → 永続化 → Entity を返す。

        precondition は型で保証済 (Ready を受けた時点で extraction 存在 +
        未 in-scope 評価 + 未 out-of-scope 評価)。AI 呼び出し中は session を保持しない
        (slow IO 中の DB 接続専有を避ける)。

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
                    in_scope_repo = InScopeRepository(session)
                    in_scope_saved = await in_scope_repo.save(
                        response,
                        ready=ready,
                        ai_model=assessor.model_name,
                    )

                    async def in_scope_audit(s: InScopeAssessment) -> None:
                        await AssessmentAuditRepository(session).append_in_scope(
                            ready=ready,
                            envelope=call,
                            assessment=s,
                            ai_model=assessor.model_name,
                            category_slug=response.category.value,
                            code="assessed_in_scope",
                        )

                    return await self._finalize(
                        session,
                        saved=in_scope_saved,
                        audit=in_scope_audit,
                        find_winner=lambda: in_scope_repo.find_by_extraction_id(
                            extraction_id
                        ),
                        extraction_id=extraction_id,
                        log_code="in_scope",
                    )

                case OutOfScope():
                    out_of_scope_repo = OutOfScopeRepository(session)
                    out_of_scope_saved = await out_of_scope_repo.save(
                        response,
                        ready=ready,
                        ai_model=assessor.model_name,
                    )

                    async def out_of_scope_audit(s: OutOfScopeAssessment) -> None:
                        await AssessmentAuditRepository(session).append_out_of_scope(
                            ready=ready,
                            envelope=call,
                            assessment=s,
                            ai_model=assessor.model_name,
                            code="assessed_out_of_scope",
                        )

                    return await self._finalize(
                        session,
                        saved=out_of_scope_saved,
                        audit=out_of_scope_audit,
                        find_winner=lambda: out_of_scope_repo.find_by_extraction_id(
                            extraction_id
                        ),
                        extraction_id=extraction_id,
                        log_code="out_of_scope",
                    )

                case _:
                    assert_never(response)

    async def _finalize[E: (InScopeAssessment, OutOfScopeAssessment)](
        self,
        session: AsyncSession,
        *,
        saved: E | None,
        audit: Callable[[E], Awaitable[None]],
        find_winner: Callable[[], Awaitable[E | None]],
        extraction_id: int,
        log_code: str,
    ) -> E:
        """save 結果を確定 Entity に解決し、winner なら audit と業務を同一 tx で
        commit。

        race lost (saved=None) は別経路として早期 return し、勝者を読み戻して返す。
        race lost path は audit を焼かず (actor SSoT — 勝者 task が自身の audit を焼く、
        二重記録回避)、commit も呼ばない (このセッションは ON CONFLICT DO NOTHING で
        0 行 + SELECT のみ、書き込みがない)。
        """
        # race lost — 勝者を読み戻して返す (audit / commit は不要)
        if saved is None:
            logger.info(
                f"assessment_{log_code}_concurrent_write",
                extraction_id=extraction_id,
            )
            winner = await find_winner()
            if winner is None:
                # ON CONFLICT で race 敗北なのに行が無い = Pattern A' 違反 / DB 異常
                raise RuntimeError(
                    f"assessment_{log_code}_race_winner_missing: "
                    f"extraction_id={extraction_id}"
                )
            return winner

        # winner — 業務 INSERT + audit を同一 tx で commit
        await audit(saved)
        await session.commit()
        logger.info(
            f"assessment_{log_code}_completed",
            extraction_id=extraction_id,
        )
        return saved
