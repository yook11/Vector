"""AssessmentRepository — Stage 4 判定結果 (in-scope / out-of-scope) の永続化。

責務:
- ``exists_in_scope`` / ``exists_out_of_scope``: ``ReadyForAssessment.try_advance_from``
  の precondition 判定用 cheap exists (extraction_id 単位)
- ``save_in_scope`` / ``save_out_of_scope``: AI 境界型 ``InScope`` / ``OutOfScope``
  を内包する ``AssessmentCall`` を受け、
  ``INSERT ... ON CONFLICT (extraction_id) DO NOTHING RETURNING id`` で
  永続化する。両 method とも DB 採番 ``id`` (``int | None``) を返し、race 敗北時
  (UNIQUE 違反) は ``None``。in-scope は category slug → id 解決を内部に閉じ、
  未登録 slug は ``AssessmentCategoryMissingError`` で fail-fast。
  Service は ``id`` のみで race 検出 + Stage 5 chain を行う (再収集は
  reconcile cron が担う)。

設計方針:
- in-scope / out-of-scope は **同じ Stage 4 永続化責務** のため、1 class に同居させて
  Service の dispatch (``match call: case AssessmentCall(result=InScope()):``) から
  対応 method を呼び分ける。ファイル分離は責務分離ではなく単に branch 表現の場所だった
  ため、AssessmentAuditRepository (1 class で in/out 両方を持つ) と signature を
  対称化する。
- ``call.model_name`` / ``call.result`` から永続化に必要な値を取り出すので、caller は
  ``ai_model`` を別引数で渡さない。AI 境界 ``InScope`` で永続化可能性を保証 → 以降は
  DB を信用、Stage 間は ID で繋ぐ (Pattern A')。Stage 5 が必要とする値は DB を
  SSoT として都度 read するため、Domain Entity を介した値運搬は廃止
  (`feedback_bc_boundary_guarantees_downstream`)。
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.schema import InScope, OutOfScope
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import AssessmentCategoryMissingError
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.out_of_scope_assessment import OutOfScopeAssessment


class AssessmentRepository:
    """Stage 4 判定結果 (in-scope / out-of-scope) の永続化 + cheap exists 判定。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- exists 判定 (try_advance_from precondition 用) -----------------------

    async def exists_in_scope(self, extraction_id: int) -> bool:
        """``try_advance_from`` 用 cheap exists 判定 (in-scope assessments)。"""
        stmt = (
            select(InScopeAssessment.id)
            .where(InScopeAssessment.extraction_id == extraction_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def exists_out_of_scope(self, extraction_id: int) -> bool:
        """``try_advance_from`` 用 cheap exists 判定 (out-of-scope assessments)。"""
        stmt = (
            select(OutOfScopeAssessment.id)
            .where(OutOfScopeAssessment.extraction_id == extraction_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    # --- save 経路 ------------------------------------------------------------

    async def save_in_scope(
        self,
        call: AssessmentCall[InScope],
        *,
        ready: ReadyForAssessment,
    ) -> int | None:
        """``AssessmentCall[InScope]`` を受けて in-scope assessment を永続化する。

        ``call.result`` / ``call.model_name`` から永続化に必要な値を直接取り出し、
        caller は ``ai_model`` を別引数で渡さない (Stage 4 で起きた事実は envelope
        が抱え切る、`feedback_bc_boundary_guarantees_downstream`)。
        ``extraction_id`` / ``translated_title`` / ``summary`` は ``ready`` から
        取り出す (Stage 3 由来 snapshot)。

        category slug → id 解決を内部化し、未登録 slug は
        ``AssessmentCategoryMissingError`` で fail-fast (Layer 2-B 業務 invariant)。
        解決後の ``category_id`` は ``in_scope_assessments.category_id`` カラムに
        INSERT するための内部使用のみで、戻り値としては外に出さない (`save_out_of_scope`
        と完全対称)。commit は呼び出し側 (Service) が行う。

        Returns:
            成功時: DB が採番した ``id``
            race 敗北時 (期待した extraction_id への UNIQUE 違反): ``None``
            (敗者は audit を焼かず短絡する — 勝者 task が自身の audit を焼く、
            audit actor SSoT 維持)

        Raises:
            ``AssessmentCategoryMissingError``: AI が catalog 未登録の slug を返した
        """
        in_scope = call.result
        category_id = await self._get_category_id_by_slug(in_scope.category.value)
        if category_id is None:
            raise AssessmentCategoryMissingError(
                f"AI returned unknown category slug: {in_scope.category.value!r}"
            )

        stmt = (
            pg_insert(InScopeAssessment)
            .values(
                extraction_id=ready.extraction_id,
                translated_title=ready.translated_title,
                summary=ready.summary,
                topic=in_scope.topic,
                category_id=category_id,
                investor_take=in_scope.investor_take,
                ai_model=call.model_name,
            )
            .on_conflict_do_nothing(index_elements=["extraction_id"])
            .returning(InScopeAssessment.id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return row.id

    async def save_out_of_scope(
        self,
        call: AssessmentCall[OutOfScope],
        *,
        ready: ReadyForAssessment,
    ) -> int | None:
        """``AssessmentCall[OutOfScope]`` を受けて out-of-scope を永続化する。

        in-scope 経路と対称: ``call.result.investor_take`` / ``call.model_name`` を
        envelope から直接取り出し、Stage 3 由来 snapshot
        (``translated_title`` / ``summary``) は ``ready`` から取り出す。
        ``out_of_scope_assessments`` には category / topic は無いので外す。

        Returns:
            成功時: DB が採番した ``id``
            race 敗北時 (期待した extraction_id への UNIQUE 違反): ``None``
        """
        out_of_scope = call.result
        stmt = (
            pg_insert(OutOfScopeAssessment)
            .values(
                extraction_id=ready.extraction_id,
                translated_title=ready.translated_title,
                summary=ready.summary,
                investor_take=out_of_scope.investor_take,
                ai_model=call.model_name,
            )
            .on_conflict_do_nothing(index_elements=["extraction_id"])
            .returning(OutOfScopeAssessment.id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return row.id

    async def _get_category_id_by_slug(self, slug: str) -> int | None:
        """カテゴリ slug から ID を取得する (Repository 内部使用)。"""
        stmt = select(Category.id).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()
