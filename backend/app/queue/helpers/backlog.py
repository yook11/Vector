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
from app.models.article_curation import ArticleCuration
from app.models.curation_noise import CurationNoise
from app.models.in_scope_assessment import InScopeAssessment
from app.models.out_of_scope_assessment import OutOfScopeAssessment


class PipelineBacklog:
    """子テーブル NULL 状態を年齢ウィンドウ + LIMIT で発見する。

    各メソッドは「発見可能な ID」のみを返し、kiq dispatch・予算消費・circuit
    breaker などの判断は呼び出し側 (cron task) の責務。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def article_ids_pending_curation(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """curation/noise いずれの子も無い未処理 Article ID を返す (Stage 2a 残)。

        ``curation_noises`` (noise 判定済み = 正常完了) も anti-join する。
        signal/noise は排他で、どちらかが在れば curation は完了している
        (precondition の ``try_load_for_curation`` と同一定義)。
        """
        stmt = (
            select(Article.id)
            .outerjoin(ArticleCuration, ArticleCuration.article_id == Article.id)
            .outerjoin(CurationNoise, CurationNoise.article_id == Article.id)
            .where(
                ArticleCuration.id.is_(None),
                CurationNoise.id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def article_ids_aged_out_curation(
        self,
        *,
        created_before: datetime,
        limit: int,
    ) -> list[int]:
        """``created_before`` より古い child-NULL Article ID を返す (救済断念対象)。

        curation/noise いずれの子も無く物理削除する対象。下限 (年齢ウィンドウの
        ``created_after``) を持たず、``article_ids_pending_curation`` の通常再投入窓
        (``[after, before)``) とは disjoint な「窓から落ちた古い記事」を拾う。
        """
        stmt = (
            select(Article.id)
            .outerjoin(ArticleCuration, ArticleCuration.article_id == Article.id)
            .outerjoin(CurationNoise, CurationNoise.article_id == Article.id)
            .where(
                ArticleCuration.id.is_(None),
                CurationNoise.id.is_(None),
                Article.created_at < created_before,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def curation_ids_pending_assessment(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """curation はあるが analysis / rejection が無い Curation ID を返す
        (Stage 2b 残)。

        article 基準の age window を維持しつつ、返却列を ``Article.id`` から
        ``ArticleCuration.id`` に変えた版 (案 3: backfill_assessments が
        ``AssessmentTrigger(curation_id=...)`` を kiq するため、Article 起点
        の 2-hop fetch は不要)。
        """
        stmt = (
            select(ArticleCuration.id)
            .join(Article, Article.id == ArticleCuration.article_id)
            .outerjoin(
                InScopeAssessment,
                InScopeAssessment.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeAssessment,
                OutOfScopeAssessment.curation_id == ArticleCuration.id,
            )
            .where(
                InScopeAssessment.id.is_(None),
                OutOfScopeAssessment.id.is_(None),
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
        """analysis はあるが embedding が NULL な Analysis ID を返す (Stage 5 残)."""
        stmt = (
            select(InScopeAssessment.id)
            .join(
                ArticleCuration,
                ArticleCuration.id == InScopeAssessment.curation_id,
            )
            .join(Article, Article.id == ArticleCuration.article_id)
            .where(
                InScopeAssessment.embedding.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
