"""OutOfScopeRepository — Stage 4 out-of-scope 評価結果の永続化と読み出し。

責務は ``InScopeRepository`` と対称 (spec §4.3.4):
- ``exists_for_extraction``: `try_advance_from` precondition 用 cheap 判定
- ``find_by_extraction_id``: race 敗北時の勝者読み戻し
- ``save``: AI 境界型 ``OutOfScope`` を受けて
  `INSERT ... ON CONFLICT (extraction_id) DO NOTHING RETURNING ...`

注 (PR3.5-d.0): Domain Entity ``OutOfScopeAssessment`` と ORM クラス
``OutOfScopeAssessment`` が同名のため、本ファイル内では ORM 側を
``OutOfScopeAssessmentORM`` alias で import して衝突回避する。
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.assessment.domain.out_of_scope import OutOfScopeAssessment
from app.analysis.classifier.schema import OutOfScope
from app.models.out_of_scope_assessment import (
    OutOfScopeAssessment as OutOfScopeAssessmentORM,
)


class OutOfScopeRepository:
    """Stage 4 out-of-scope 評価結果の永続化に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_for_extraction(self, extraction_id: int) -> bool:
        """`try_advance_from` 用 cheap exists 判定 (extraction_id 単位)。"""
        stmt = (
            select(OutOfScopeAssessmentORM.id)
            .where(OutOfScopeAssessmentORM.extraction_id == extraction_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def find_by_extraction_id(
        self, extraction_id: int
    ) -> OutOfScopeAssessment | None:
        """既存 out-of-scope 評価を Entity として取得する (race 敗北時の読み戻し)。"""
        stmt = select(OutOfScopeAssessmentORM).where(
            OutOfScopeAssessmentORM.extraction_id == extraction_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def save(
        self,
        out_of_scope: OutOfScope,
        *,
        extraction_id: int,
        ai_model: str,
    ) -> OutOfScopeAssessment | None:
        """AI 境界型を受けて ``INSERT ... ON CONFLICT (extraction_id) DO NOTHING
        RETURNING ...`` で永続化する。

        Returns:
            成功時: 永続化された ``OutOfScopeAssessment`` Entity
            race 敗北時 (期待した extraction_id への UNIQUE 違反): ``None``
        """
        stmt = (
            pg_insert(OutOfScopeAssessmentORM)
            .values(
                extraction_id=extraction_id,
                investor_take=out_of_scope.investor_take,
                ai_model=ai_model,
            )
            .on_conflict_do_nothing(index_elements=["extraction_id"])
            .returning(OutOfScopeAssessmentORM.id, OutOfScopeAssessmentORM.rejected_at)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return OutOfScopeAssessment(
            id=row.id,
            extraction_id=extraction_id,
            investor_take=out_of_scope.investor_take,
            ai_model=ai_model,
            rejected_at=row.rejected_at,
        )

    @staticmethod
    def _to_domain(orm: OutOfScopeAssessmentORM) -> OutOfScopeAssessment:
        """ORM から記録済み Entity へ復元する。"""
        return OutOfScopeAssessment(
            id=orm.id,
            extraction_id=orm.extraction_id,
            investor_take=orm.investor_take,
            ai_model=orm.ai_model,
            rejected_at=orm.rejected_at,
        )
