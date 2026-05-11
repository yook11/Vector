"""OutOfScopeRepository — Stage 4 out-of-scope 評価結果の永続化。

責務は ``InScopeRepository`` と対称 (spec §4.3.4):
- ``exists_for_extraction``: `try_advance_from` precondition 用 cheap 判定
- ``save``: AI 境界型 ``OutOfScope`` を受けて
  `INSERT ... ON CONFLICT (extraction_id) DO NOTHING RETURNING ...`

設計方針 (2026-05-11 更新): Stage 4 で永続化が確定したら以降は DB を SSoT として
信用するため、Domain Entity 経由の値運搬は廃止。``save`` の戻り値は audit 焼付に
必要な最小情報 (新規 row id) のみに絞る
(`feedback_bc_boundary_guarantees_downstream`)。
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.assessment.ai.schema import OutOfScope
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.models.out_of_scope_assessment import OutOfScopeAssessment


class OutOfScopeRepository:
    """Stage 4 out-of-scope 評価結果の永続化に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_for_extraction(self, extraction_id: int) -> bool:
        """`try_advance_from` 用 cheap exists 判定 (extraction_id 単位)。"""
        stmt = (
            select(OutOfScopeAssessment.id)
            .where(OutOfScopeAssessment.extraction_id == extraction_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def save(
        self,
        out_of_scope: OutOfScope,
        *,
        ready: ReadyForAssessment,
        ai_model: str,
    ) -> int | None:
        """AI 境界型 + ``ReadyForAssessment`` (Stage 3 由来 snapshot) を受けて
        ``INSERT ... ON CONFLICT (extraction_id) DO NOTHING RETURNING ...`` で
        永続化する。

        ``extraction_id`` / ``translated_title`` / ``summary`` は ``ready`` から取り出す
        (``InScopeRepository.save`` と signature 完全対称)。``translated_title`` /
        ``summary`` は in-scope 経路と対称な point-in-time snapshot で、AI 境界型
        ``OutOfScope`` には含まれないため ``ready`` 経由で受け取る。

        Returns:
            成功時: DB が採番した ``id``
            race 敗北時 (期待した extraction_id への UNIQUE 違反): ``None``
        """
        stmt = (
            pg_insert(OutOfScopeAssessment)
            .values(
                extraction_id=ready.extraction_id,
                translated_title=ready.translated_title,
                summary=ready.summary,
                investor_take=out_of_scope.investor_take,
                ai_model=ai_model,
            )
            .on_conflict_do_nothing(index_elements=["extraction_id"])
            .returning(OutOfScopeAssessment.id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return row.id
