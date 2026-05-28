"""back-fill 対象 Article ID のクエリ (Repository)。

メインフローで諦め return された結果として下流子テーブルが NULL になっている
記事を、年齢ウィンドウの範囲で発見する。SQL は SQLAlchemy 2.0 スタイルで
組み立て、文字列結合や生 SQL は使わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    EmbeddingBackfillExclusion,
)
from app.models.curation_noise import CurationNoise
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.out_of_scope_assessment import OutOfScopeAssessment


@dataclass(frozen=True, slots=True)
class BackfillTarget:
    """backfill が enqueue と監査に使う対象 snapshot。"""

    target_id: int
    article_id: int
    source_name: str | None


class PipelineBacklog:
    """子テーブル NULL 状態を年齢ウィンドウで発見する。

    ID 取得メソッド (``*_pending_*``) は LIMIT 付きで dispatch 対象を返す。
    COUNT メソッド (``count_*``) は LIMIT なしの真の総数を Logfire gauge
    観測用に返す (dispatch list の ``len()`` は LIMIT で saturate するため、
    詰まりの可視化には COUNT 経路が必要)。kiq dispatch・予算消費の判断は
    呼び出し側 (cron task) の責務。
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

    async def curation_targets_pending(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[BackfillTarget]:
        """Stage 3 backfill の enqueue / audit 対象を返す。"""
        stmt = (
            select(Article.id, Article.id, NewsSource.name)
            .outerjoin(NewsSource, NewsSource.id == Article.source_id)
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
        rows = (await self._session.execute(stmt)).tuples().all()
        return [_target_from_row(row) for row in rows]

    async def count_articles_pending_curation(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> int:
        """curation/noise 未処理 Article の真の総数 (LIMIT なし COUNT)。観測専用。"""
        stmt = (
            select(func.count(Article.id))
            .outerjoin(ArticleCuration, ArticleCuration.article_id == Article.id)
            .outerjoin(CurationNoise, CurationNoise.article_id == Article.id)
            .where(
                ArticleCuration.id.is_(None),
                CurationNoise.id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

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

    async def assessment_targets_pending(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[BackfillTarget]:
        """Stage 4 backfill の enqueue / audit 対象を返す。"""
        stmt = (
            select(ArticleCuration.id, ArticleCuration.article_id, NewsSource.name)
            .join(Article, Article.id == ArticleCuration.article_id)
            .outerjoin(NewsSource, NewsSource.id == Article.source_id)
            .outerjoin(
                InScopeAssessment,
                InScopeAssessment.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeAssessment,
                OutOfScopeAssessment.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                AssessmentBackfillExclusion,
                AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
            )
            .where(
                InScopeAssessment.id.is_(None),
                OutOfScopeAssessment.id.is_(None),
                AssessmentBackfillExclusion.curation_id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [_target_from_row(row) for row in rows]

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
            .outerjoin(
                AssessmentBackfillExclusion,
                AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
            )
            .where(
                InScopeAssessment.id.is_(None),
                OutOfScopeAssessment.id.is_(None),
                AssessmentBackfillExclusion.curation_id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_curations_pending_assessment(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> int:
        """assessment 未処理 curation の真の総数 (LIMIT なし COUNT)。観測専用。

        ``curation_ids_pending_assessment`` と同じ JOIN / where 条件を共有
        するが、LIMIT を持たない。Logfire gauge への observability 用途で、
        dispatch とは別経路 (dispatch は ``ASSESSMENTS_LIMIT`` で頭打ち、観測は
        それを超えた真値を出す)。

        同一 ``AsyncSession`` 内で COUNT → ID 取得を順に呼ぶことで read
        committed snapshot 上で一貫した値を返す (並行 INSERT/DELETE による
        僅かな乖離は観測値として許容)。
        """
        stmt = (
            select(func.count(ArticleCuration.id))
            .join(Article, Article.id == ArticleCuration.article_id)
            .outerjoin(
                InScopeAssessment,
                InScopeAssessment.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeAssessment,
                OutOfScopeAssessment.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                AssessmentBackfillExclusion,
                AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
            )
            .where(
                InScopeAssessment.id.is_(None),
                OutOfScopeAssessment.id.is_(None),
                AssessmentBackfillExclusion.curation_id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def curation_ids_aged_out_assessment(
        self,
        *,
        created_before: datetime,
        limit: int,
    ) -> list[int]:
        """通常窓から落ちた assessment 未完了 Curation ID を返す。

        Stage 4/5 は保全価値のある部分結果を持つため物理削除せず、呼び出し側が
        ``assessment_backfill_exclusions`` に current-state sentinel を作る。
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
            .outerjoin(
                AssessmentBackfillExclusion,
                AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
            )
            .where(
                InScopeAssessment.id.is_(None),
                OutOfScopeAssessment.id.is_(None),
                AssessmentBackfillExclusion.curation_id.is_(None),
                Article.created_at < created_before,
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
            .outerjoin(
                EmbeddingBackfillExclusion,
                EmbeddingBackfillExclusion.analysis_id == InScopeAssessment.id,
            )
            .where(
                InScopeAssessment.embedding.is_(None),
                EmbeddingBackfillExclusion.analysis_id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def embedding_targets_pending(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[BackfillTarget]:
        """Stage 5 backfill の enqueue / audit 対象を返す。"""
        stmt = (
            select(InScopeAssessment.id, ArticleCuration.article_id, NewsSource.name)
            .join(
                ArticleCuration,
                ArticleCuration.id == InScopeAssessment.curation_id,
            )
            .join(Article, Article.id == ArticleCuration.article_id)
            .outerjoin(NewsSource, NewsSource.id == Article.source_id)
            .outerjoin(
                EmbeddingBackfillExclusion,
                EmbeddingBackfillExclusion.analysis_id == InScopeAssessment.id,
            )
            .where(
                InScopeAssessment.embedding.is_(None),
                EmbeddingBackfillExclusion.analysis_id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [_target_from_row(row) for row in rows]

    async def count_analyses_pending_embedding(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> int:
        """embedding NULL analysis の真の総数 (LIMIT なし COUNT)。観測専用。"""
        stmt = (
            select(func.count(InScopeAssessment.id))
            .join(
                ArticleCuration,
                ArticleCuration.id == InScopeAssessment.curation_id,
            )
            .join(Article, Article.id == ArticleCuration.article_id)
            .outerjoin(
                EmbeddingBackfillExclusion,
                EmbeddingBackfillExclusion.analysis_id == InScopeAssessment.id,
            )
            .where(
                InScopeAssessment.embedding.is_(None),
                EmbeddingBackfillExclusion.analysis_id.is_(None),
                Article.created_at < created_before,
                Article.created_at >= created_after,
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def analysis_ids_aged_out_embedding(
        self,
        *,
        created_before: datetime,
        limit: int,
    ) -> list[int]:
        """通常窓から落ちた embedding NULL Analysis ID を返す。"""
        stmt = (
            select(InScopeAssessment.id)
            .join(
                ArticleCuration,
                ArticleCuration.id == InScopeAssessment.curation_id,
            )
            .join(Article, Article.id == ArticleCuration.article_id)
            .outerjoin(
                EmbeddingBackfillExclusion,
                EmbeddingBackfillExclusion.analysis_id == InScopeAssessment.id,
            )
            .where(
                InScopeAssessment.embedding.is_(None),
                EmbeddingBackfillExclusion.analysis_id.is_(None),
                Article.created_at < created_before,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


def _target_from_row(row: tuple[int, int, object | None]) -> BackfillTarget:
    target_id, article_id, source_name = row
    return BackfillTarget(
        target_id=target_id,
        article_id=article_id,
        source_name=str(source_name) if source_name is not None else None,
    )
