"""AnalysisRepository — Stage 2 Classified の永続化と読み出し。

責務:

- ``save``: ``AnalysisDraft`` を永続化し、DB が付与した identity
  (``PersistedAnalysisId``) を返す。Entity の組み立ては呼び出し側
  (``Analysis.from_draft``) に任せる。
- ``find_by_extraction_id``: ORM 行をドメイン Entity (``Analysis``) として復元する。
- ``get_category_id_by_slug``: AI が返した category slug から FK 用 id を解決する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.classification.domain.analysis import Analysis, AnalysisDraft
from app.models.article_analysis import ArticleAnalysis
from app.models.category import Category


@dataclass(frozen=True, slots=True)
class PersistedAnalysisId:
    """永続化で DB が付与した identity。

    ``save`` の戻り値。呼び出し側はこの値と元の ``AnalysisDraft`` を
    ``Analysis.from_draft`` に渡して記録済み Entity を組み立てる。
    """

    id: int
    analyzed_at: datetime


class AnalysisRepository:
    """Stage 2 Classified の永続化に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_extraction_id(self, extraction_id: int) -> Analysis | None:
        """extraction に紐づく分析結果を Entity として取得する。冪等性チェック兼用。"""
        stmt = select(ArticleAnalysis).where(
            ArticleAnalysis.extraction_id == extraction_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def save(
        self,
        draft: AnalysisDraft,
        *,
        extraction_id: int,
        category_id: int,
        ai_model: str,
    ) -> PersistedAnalysisId:
        """Draft を永続化し、DB が採番した identity を返す。

        commit は呼び出し側 (Service) が行う。``analyzed_at`` は server_default
        により DB が確定させるため refresh で取得する。
        """
        orm = ArticleAnalysis(
            extraction_id=extraction_id,
            translated_title=draft.translated_title,
            summary=draft.summary,
            topic=draft.topic_name,
            category_id=category_id,
            investor_take=draft.investor_take,
            ai_model=ai_model,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm, attribute_names=["analyzed_at"])
        return PersistedAnalysisId(id=orm.id, analyzed_at=orm.analyzed_at)

    async def get_category_id_by_slug(self, slug: str) -> int | None:
        """カテゴリ slug から ID を取得する。"""
        stmt = select(Category.id).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _to_domain(orm: ArticleAnalysis) -> Analysis:
        """ORM から記録済み Entity へ復元する。"""
        return Analysis(
            id=orm.id,
            extraction_id=orm.extraction_id,
            translated_title=orm.translated_title,
            summary=orm.summary,
            topic=orm.topic,
            category_id=orm.category_id,
            investor_take=orm.investor_take,
            ai_model=orm.ai_model,
            analyzed_at=orm.analyzed_at,
        )
