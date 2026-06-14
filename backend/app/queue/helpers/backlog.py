"""back-fill 対象 AnalyzableArticleRecord ID のクエリ (Repository)。

メインフローで諦め return された結果として下流子テーブルが NULL になっている
記事を、年齢ウィンドウの範囲で発見する。SQL は SQLAlchemy 2.0 スタイルで
組み立て、文字列結合や生 SQL は使わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    EmbeddingBackfillExclusion,
)
from app.models.curation_noise import CurationNoise
from app.models.news_source import NewsSource
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord


@dataclass(frozen=True, slots=True)
class BackfillTarget:
    """backfill が enqueue と監査に使う対象 snapshot。"""

    target_id: int
    analyzable_article_id: int
    source_name: str | None


class PipelineBacklog:
    """子テーブル NULL 状態を年齢ウィンドウで発見する。

    ID 取得メソッド (``*_pending_*``) は LIMIT 付きで dispatch 対象を返す。
    COUNT メソッド (``count_*``) は LIMIT なしの真の総数を Logfire gauge
    観測用に返す (dispatch list の ``len()`` は LIMIT で saturate するため、
    詰まりの可視化には COUNT 経路が必要)。stats メソッド (``*_pending_*_stats``)
    は同述語の ``(総数, 最古 created_at)`` を 1 クエリで返す (health endpoint 用)。
    3 系統は stage ごとの ``_*_pending`` 述語ビルダを共有する。kiq dispatch・
    予算消費の判断は呼び出し側 (cron task) の責務。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- pending 述語ビルダ (ids / count / stats が共有する SQL 本体の単一定義) ---
    # 各 stage の FROM + JOIN + anti-join + 年齢窓だけを組み立て、SELECT 列は呼び出し側
    # が渡す。``select_from`` を明示し ``func.min(AnalyzableArticleRecord.created_at)``
    # 系の FROM 推論ずれも固定する。``*_targets_pending`` (NewsSource 付き) /
    # ``*_aged_out_*``
    # (下限なし) は述語が分岐するため共有しない。

    def _curation_pending(
        self, stmt: Select[Any], *, created_before: datetime, created_after: datetime
    ) -> Select[Any]:
        return (
            stmt.select_from(AnalyzableArticleRecord)
            .outerjoin(
                ArticleCuration,
                ArticleCuration.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .outerjoin(
                CurationNoise,
                CurationNoise.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .where(
                ArticleCuration.id.is_(None),
                CurationNoise.id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                AnalyzableArticleRecord.created_at >= created_after,
            )
        )

    def _assessment_pending(
        self, stmt: Select[Any], *, created_before: datetime, created_after: datetime
    ) -> Select[Any]:
        return (
            stmt.select_from(ArticleCuration)
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(
                AnalyzedArticleRecord,
                AnalyzedArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeArticleRecord,
                OutOfScopeArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                AssessmentBackfillExclusion,
                AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
            )
            .where(
                AnalyzedArticleRecord.id.is_(None),
                OutOfScopeArticleRecord.id.is_(None),
                AssessmentBackfillExclusion.curation_id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                AnalyzableArticleRecord.created_at >= created_after,
            )
        )

    def _embedding_pending(
        self, stmt: Select[Any], *, created_before: datetime, created_after: datetime
    ) -> Select[Any]:
        return (
            stmt.select_from(AnalyzedArticleRecord)
            .join(
                ArticleCuration,
                ArticleCuration.id == AnalyzedArticleRecord.curation_id,
            )
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(
                EmbeddingBackfillExclusion,
                EmbeddingBackfillExclusion.analyzed_article_id
                == AnalyzedArticleRecord.id,
            )
            .where(
                AnalyzedArticleRecord.embedding.is_(None),
                EmbeddingBackfillExclusion.analyzed_article_id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                AnalyzableArticleRecord.created_at >= created_after,
            )
        )

    async def analyzable_article_ids_pending_curation(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """curation/noise いずれの子も無い未処理 article record ID を返す。

        ``curation_noises`` (noise 判定済み = 正常完了) も anti-join する。
        signal/noise は排他で、どちらかが在れば curation は完了している
        (``ReadyForCuration.try_advance_from`` の precondition と同一定義)。
        """
        stmt = (
            self._curation_pending(
                select(AnalyzableArticleRecord.id),
                created_before=created_before,
                created_after=created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
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
            select(
                AnalyzableArticleRecord.id, AnalyzableArticleRecord.id, NewsSource.name
            )
            .outerjoin(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
            .outerjoin(
                ArticleCuration,
                ArticleCuration.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .outerjoin(
                CurationNoise,
                CurationNoise.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .where(
                ArticleCuration.id.is_(None),
                CurationNoise.id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                AnalyzableArticleRecord.created_at >= created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
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
        """curation/noise 未処理 article record の真の総数を返す。"""
        stmt = self._curation_pending(
            select(func.count(AnalyzableArticleRecord.id)),
            created_before=created_before,
            created_after=created_after,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def articles_pending_curation_stats(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> tuple[int, datetime | None]:
        """curation/noise 未処理 article record の stats を返す。

        ``count_articles_pending_curation`` と同一述語を 1 クエリで COUNT + MIN する
        (health endpoint が count と最古を別クエリに分けないため)。最古 = dispatch 順
        (``created_at`` asc) の先頭。対象なしは ``(0, None)``。
        """
        stmt = self._curation_pending(
            select(
                func.count(AnalyzableArticleRecord.id),
                func.min(AnalyzableArticleRecord.created_at),
            ),
            created_before=created_before,
            created_after=created_after,
        )
        row = (await self._session.execute(stmt)).one()
        return int(row[0]), row[1]

    async def analyzable_article_ids_aged_out_curation(
        self,
        *,
        created_before: datetime,
        limit: int,
    ) -> list[int]:
        """``created_before`` より古い child-NULL article record ID を返す。

        curation/noise いずれの子も無く物理削除する対象。下限 (年齢ウィンドウの
        ``created_after``) を持たず、``analyzable_article_ids_pending_curation`` の
        通常再投入窓 (``[after, before)``) とは disjoint な
        「窓から落ちた古い記事」を拾う。
        """
        stmt = (
            select(AnalyzableArticleRecord.id)
            .outerjoin(
                ArticleCuration,
                ArticleCuration.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .outerjoin(
                CurationNoise,
                CurationNoise.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .where(
                ArticleCuration.id.is_(None),
                CurationNoise.id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
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
            select(
                ArticleCuration.id,
                ArticleCuration.analyzable_article_id,
                NewsSource.name,
            )
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
            .outerjoin(
                AnalyzedArticleRecord,
                AnalyzedArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeArticleRecord,
                OutOfScopeArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                AssessmentBackfillExclusion,
                AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
            )
            .where(
                AnalyzedArticleRecord.id.is_(None),
                OutOfScopeArticleRecord.id.is_(None),
                AssessmentBackfillExclusion.curation_id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                AnalyzableArticleRecord.created_at >= created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
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
        """curation はあるが analysis / rejection が無い Curation ID を返す。"""
        stmt = (
            self._assessment_pending(
                select(ArticleCuration.id),
                created_before=created_before,
                created_after=created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
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
        stmt = self._assessment_pending(
            select(func.count(ArticleCuration.id)),
            created_before=created_before,
            created_after=created_after,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def curations_pending_assessment_stats(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> tuple[int, datetime | None]:
        """assessment 未処理 curation の ``(総数, 最古 created_at)`` (観測専用)。

        ``count_curations_pending_assessment`` と同一述語を 1 クエリで COUNT+MIN。
        対象なしは ``(0, None)``。
        """
        stmt = self._assessment_pending(
            select(
                func.count(ArticleCuration.id),
                func.min(AnalyzableArticleRecord.created_at),
            ),
            created_before=created_before,
            created_after=created_after,
        )
        row = (await self._session.execute(stmt)).one()
        return int(row[0]), row[1]

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
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(
                AnalyzedArticleRecord,
                AnalyzedArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeArticleRecord,
                OutOfScopeArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                AssessmentBackfillExclusion,
                AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
            )
            .where(
                AnalyzedArticleRecord.id.is_(None),
                OutOfScopeArticleRecord.id.is_(None),
                AssessmentBackfillExclusion.curation_id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def analyzed_article_ids_pending_embedding(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """embedding が NULL な AnalyzedArticleRecord ID を返す (Stage 5 残)."""
        stmt = (
            self._embedding_pending(
                select(AnalyzedArticleRecord.id),
                created_before=created_before,
                created_after=created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
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
            select(
                AnalyzedArticleRecord.id,
                ArticleCuration.analyzable_article_id,
                NewsSource.name,
            )
            .join(
                ArticleCuration,
                ArticleCuration.id == AnalyzedArticleRecord.curation_id,
            )
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
            .outerjoin(
                EmbeddingBackfillExclusion,
                EmbeddingBackfillExclusion.analyzed_article_id
                == AnalyzedArticleRecord.id,
            )
            .where(
                AnalyzedArticleRecord.embedding.is_(None),
                EmbeddingBackfillExclusion.analyzed_article_id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                AnalyzableArticleRecord.created_at >= created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [_target_from_row(row) for row in rows]

    async def count_analyzed_articles_pending_embedding(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> int:
        """embedding NULL analyzed article の真の総数 (LIMIT なし COUNT)。"""
        stmt = self._embedding_pending(
            select(func.count(AnalyzedArticleRecord.id)),
            created_before=created_before,
            created_after=created_after,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def analyzed_articles_pending_embedding_stats(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> tuple[int, datetime | None]:
        """embedding NULL analyzed article の stats を返す。

        ``count_analyzed_articles_pending_embedding`` と同一述語を 1 クエリで
        COUNT + MIN する。
        対象なしは ``(0, None)``。
        """
        stmt = self._embedding_pending(
            select(
                func.count(AnalyzedArticleRecord.id),
                func.min(AnalyzableArticleRecord.created_at),
            ),
            created_before=created_before,
            created_after=created_after,
        )
        row = (await self._session.execute(stmt)).one()
        return int(row[0]), row[1]

    async def analyzed_article_ids_aged_out_embedding(
        self,
        *,
        created_before: datetime,
        limit: int,
    ) -> list[int]:
        """通常窓から落ちた embedding NULL AnalyzedArticleRecord ID を返す。"""
        stmt = (
            select(AnalyzedArticleRecord.id)
            .join(
                ArticleCuration,
                ArticleCuration.id == AnalyzedArticleRecord.curation_id,
            )
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(
                EmbeddingBackfillExclusion,
                EmbeddingBackfillExclusion.analyzed_article_id
                == AnalyzedArticleRecord.id,
            )
            .where(
                AnalyzedArticleRecord.embedding.is_(None),
                EmbeddingBackfillExclusion.analyzed_article_id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


def _target_from_row(row: tuple[int, int, object | None]) -> BackfillTarget:
    target_id, analyzable_article_id, source_name = row
    return BackfillTarget(
        target_id=target_id,
        analyzable_article_id=analyzable_article_id,
        source_name=str(source_name) if source_name is not None else None,
    )
