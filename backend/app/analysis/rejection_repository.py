"""Rejection リポジトリ — OutOfScope 判定の永続化を担う。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article_rejection import ArticleRejection


class RejectionRepository:
    """分類対象外（OutOfScope）の永続化に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_extraction_id(
        self, extraction_id: int
    ) -> ArticleRejection | None:
        """extraction に紐づく既存の rejection を取得する（冪等性チェック兼用）。"""
        stmt = select(ArticleRejection).where(
            ArticleRejection.extraction_id == extraction_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def save_rejection(self, rejection: ArticleRejection) -> ArticleRejection:
        """rejection を永続化する（flush のみ、commit しない）。"""
        self._session.add(rejection)
        await self._session.flush()
        return rejection
