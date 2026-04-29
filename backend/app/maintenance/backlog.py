"""back-fill 対象 Article ID のクエリ (Repository)。

メインフローで諦め return された結果として下流子テーブルが NULL になっている
記事を、年齢ウィンドウの範囲で発見する。SQL は SQLAlchemy 2.0 スタイルで
組み立て、文字列結合や生 SQL は使わない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction
from app.models.article_rejection import ArticleRejection


class PipelineBacklog:
    """子テーブル NULL 状態を年齢ウィンドウ + LIMIT で発見する。

    各メソッドは「発見可能な ID」のみを返し、kiq dispatch・予算消費・circuit
    breaker などの判断は呼び出し側 (cron task) の責務。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def article_ids_pending_extraction(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """``article_extractions`` の子が無い Article ID を返す (Stage 2a 残)."""
        stmt = (
            select(Article.id)
            .outerjoin(ArticleExtraction, ArticleExtraction.article_id == Article.id)
            .where(
                ArticleExtraction.id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def article_ids_pending_classification(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """extraction はあるが analysis / rejection が無い ID を返す (Stage 2b 残)."""
        stmt = (
            select(Article.id)
            .join(ArticleExtraction, ArticleExtraction.article_id == Article.id)
            .outerjoin(
                ArticleAnalysis,
                ArticleAnalysis.extraction_id == ArticleExtraction.id,
            )
            .outerjoin(
                ArticleRejection,
                ArticleRejection.extraction_id == ArticleExtraction.id,
            )
            .where(
                ArticleAnalysis.id.is_(None),
                ArticleRejection.id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def analysis_ids_pending_embedding(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """analysis はあるが embedding が NULL な Analysis ID を返す (Stage E 残)."""
        stmt = (
            select(ArticleAnalysis.id)
            .join(
                ArticleExtraction,
                ArticleExtraction.id == ArticleAnalysis.extraction_id,
            )
            .join(Article, Article.id == ArticleExtraction.article_id)
            .where(
                ArticleAnalysis.embedding.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
