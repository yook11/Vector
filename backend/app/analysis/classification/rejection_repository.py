"""RejectionRepository — Stage D OutOfScope の永続化と読み出し。

責務は ``AnalysisRepository`` と対称 (spec §4.3.4):
- ``exists_for_extraction``: `try_advance_from` precondition 用 cheap 判定
- ``find_by_extraction_id``: race 敗北時の勝者読み戻し
- ``save``: `INSERT ... ON CONFLICT (extraction_id) DO NOTHING RETURNING ...`
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.classification.domain.rejection import Rejection, RejectionDraft
from app.models.article_rejection import ArticleRejection


class RejectionRepository:
    """Stage D OutOfScope の永続化に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_for_extraction(self, extraction_id: int) -> bool:
        """`try_advance_from` 用 cheap exists 判定 (extraction_id 単位)。"""
        stmt = (
            select(ArticleRejection.id)
            .where(ArticleRejection.extraction_id == extraction_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def find_by_extraction_id(self, extraction_id: int) -> Rejection | None:
        """既存 rejection を Entity として取得する (race 敗北時の読み戻し)。"""
        stmt = select(ArticleRejection).where(
            ArticleRejection.extraction_id == extraction_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def save(
        self,
        draft: RejectionDraft,
        *,
        extraction_id: int,
        ai_model: str,
    ) -> Rejection | None:
        """Draft を ``INSERT ... ON CONFLICT (extraction_id) DO NOTHING RETURNING ...``
        で永続化する。

        Returns:
            成功時: 永続化された ``Rejection`` Entity
            race 敗北時 (期待した extraction_id への UNIQUE 違反): ``None``
        """
        stmt = (
            pg_insert(ArticleRejection)
            .values(
                extraction_id=extraction_id,
                investor_take=draft.investor_take,
                ai_model=ai_model,
            )
            .on_conflict_do_nothing(index_elements=["extraction_id"])
            .returning(ArticleRejection.id, ArticleRejection.rejected_at)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return Rejection(
            id=row.id,
            extraction_id=extraction_id,
            investor_take=draft.investor_take,
            ai_model=ai_model,
            rejected_at=row.rejected_at,
        )

    @staticmethod
    def _to_domain(orm: ArticleRejection) -> Rejection:
        """ORM から記録済み Entity へ復元する。"""
        return Rejection(
            id=orm.id,
            extraction_id=orm.extraction_id,
            investor_take=orm.investor_take,
            ai_model=orm.ai_model,
            rejected_at=orm.rejected_at,
        )
