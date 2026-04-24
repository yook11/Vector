"""RejectionRepository — Stage 2 OutOfScope の永続化と読み出し。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.classification.domain.rejection import Rejection, RejectionDraft
from app.models.article_rejection import ArticleRejection


@dataclass(frozen=True, slots=True)
class PersistedRejectionId:
    """永続化で DB が付与した identity。

    ``save`` の戻り値。呼び出し側はこの値と元の ``RejectionDraft`` を
    ``Rejection.from_draft`` に渡して記録済み Entity を組み立てる。
    """

    id: int
    rejected_at: datetime


class RejectionRepository:
    """Stage 2 OutOfScope の永続化に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_extraction_id(self, extraction_id: int) -> Rejection | None:
        """既存 rejection を Entity として取得する (冪等性チェック兼用)。"""
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
    ) -> PersistedRejectionId:
        """Draft を永続化し、DB が採番した identity を返す。

        commit は呼び出し側 (Service) が行う。``rejected_at`` は server_default
        により DB が確定させるため refresh で取得する。
        """
        orm = ArticleRejection(
            extraction_id=extraction_id,
            reasoning=draft.reasoning,
            ai_model=ai_model,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm, attribute_names=["rejected_at"])
        return PersistedRejectionId(id=orm.id, rejected_at=orm.rejected_at)

    @staticmethod
    def _to_domain(orm: ArticleRejection) -> Rejection:
        """ORM から記録済み Entity へ復元する。"""
        return Rejection(
            id=orm.id,
            extraction_id=orm.extraction_id,
            reasoning=orm.reasoning,
            ai_model=orm.ai_model,
            rejected_at=orm.rejected_at,
        )
