"""completion の pending 読み出し・状態遷移・永続化境界。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.article_completion.ready import (
    ArticleCompletionReadyBuildFacts,
    ReadyForArticleCompletion,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.persistence.analyzable_article_repository import (
    AnalyzableArticleRepository,
)
from app.models.incomplete_article import IncompleteArticle as IncompleteArticleORM


@dataclass(frozen=True, slots=True)
class CompletionSucceeded:
    """正規所有者として ``analyzable_articles`` に INSERT 成功。"""

    analyzable_article_id: int


@dataclass(frozen=True, slots=True)
class CompletionSuperseded:
    """別 worker に追い越され、自分の attempt は失効していた (pending DELETE 0 行)。"""


@dataclass(frozen=True, slots=True)
class CompletionUrlConflict:
    """attempt は有効だが ``source_url`` 衝突で race-loss。"""


CompletionOutcome = CompletionSucceeded | CompletionSuperseded | CompletionUrlConflict


class ArticleCompletionRepository:
    """補完に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_ready_build_facts(
        self, pending_id: int
    ) -> ArticleCompletionReadyBuildFacts | None:
        """Ready 構築に必要な pending 行の DB 事実を取得する。"""
        stmt = (
            select(
                IncompleteArticleORM.id,
                IncompleteArticleORM.source_id,
                IncompleteArticleORM.source_name,
                IncompleteArticleORM.status,
                IncompleteArticleORM.observed_article,
                IncompleteArticleORM.url,
                IncompleteArticleORM.attempt_count,
            )
            .where(IncompleteArticleORM.id == pending_id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        (
            row_id,
            source_id,
            source_name,
            status,
            observed_article,
            source_url,
            attempt_count,
        ) = row
        return ArticleCompletionReadyBuildFacts(
            pending_id=row_id,
            source_id=source_id,
            source_name=source_name,
            status=status,
            observed_article=dict(observed_article or {}),
            source_url=str(source_url),
            attempt_count=attempt_count,
        )

    async def claim_ready_batch(
        self,
        *,
        limit: int,
        now: datetime,
        leased_until: datetime,
    ) -> list[int]:
        """ready な open pending を claim し、claim できた id を返す。"""
        if limit <= 0:
            return []

        select_stmt = (
            select(IncompleteArticleORM.id)
            .where(
                IncompleteArticleORM.status == "open",
                IncompleteArticleORM.ready_at <= now,
            )
            .order_by(IncompleteArticleORM.ready_at, IncompleteArticleORM.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        ids = list((await self._session.execute(select_stmt)).scalars().all())
        if not ids:
            return []

        update_stmt = (
            update(IncompleteArticleORM)
            .where(
                IncompleteArticleORM.id.in_(ids),
                IncompleteArticleORM.status == "open",
            )
            .values(
                status="running",
                leased_until=leased_until,
                attempt_count=IncompleteArticleORM.attempt_count + 1,
                updated_at=now,
            )
            .returning(IncompleteArticleORM.id)
        )
        updated_ids = set((await self._session.execute(update_stmt)).scalars().all())
        return [pending_id for pending_id in ids if pending_id in updated_ids]

    async def sweep_expired_leases(self, *, now: datetime) -> int:
        """期限切れ lease の ``running`` 行を ``open`` に戻す。"""
        stmt = (
            update(IncompleteArticleORM)
            .where(
                IncompleteArticleORM.status == "running",
                IncompleteArticleORM.leased_until <= now,
            )
            .values(
                status="open",
                ready_at=now,
                leased_until=None,
                updated_at=now,
            )
            .returning(IncompleteArticleORM.id)
        )
        rows = (await self._session.execute(stmt)).all()
        return len(rows)

    async def close_claimed(
        self,
        ready: ReadyForArticleCompletion,
        *,
        now: datetime,
    ) -> bool:
        """現在の attempt がまだ有効なら ``closed`` に閉じる。"""
        stmt = (
            update(IncompleteArticleORM)
            .where(
                IncompleteArticleORM.id == ready.pending_id,
                IncompleteArticleORM.status == "running",
                IncompleteArticleORM.attempt_count == ready.attempt_count,
            )
            .values(status="closed", leased_until=None, updated_at=now)
            .returning(IncompleteArticleORM.id)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def schedule_retry(
        self,
        ready: ReadyForArticleCompletion,
        *,
        ready_at: datetime,
        now: datetime,
    ) -> bool:
        """現在の attempt がまだ有効なら ``open`` に戻し、次回時刻を設定する。"""
        stmt = (
            update(IncompleteArticleORM)
            .where(
                IncompleteArticleORM.id == ready.pending_id,
                IncompleteArticleORM.status == "running",
                IncompleteArticleORM.attempt_count == ready.attempt_count,
            )
            .values(
                status="open",
                ready_at=ready_at,
                leased_until=None,
                updated_at=now,
            )
            .returning(IncompleteArticleORM.id)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def persist_completed(
        self,
        ready: ReadyForArticleCompletion,
        advanced: AnalyzableArticle,
    ) -> CompletionOutcome:
        """有効な attempt だけを article に昇格し、race outcome を値で返す。"""
        if not await self._delete_claimed(ready):
            return CompletionSuperseded()

        analyzable_article_id = await AnalyzableArticleRepository(self._session).save(
            advanced
        )
        if analyzable_article_id is None:
            return CompletionUrlConflict()
        return CompletionSucceeded(analyzable_article_id=analyzable_article_id)

    async def _delete_claimed(self, ready: ReadyForArticleCompletion) -> bool:
        stmt = (
            delete(IncompleteArticleORM)
            .where(
                IncompleteArticleORM.id == ready.pending_id,
                IncompleteArticleORM.status == "running",
                IncompleteArticleORM.attempt_count == ready.attempt_count,
            )
            .returning(IncompleteArticleORM.id)
        )
        return (await self._session.execute(stmt)).first() is not None
