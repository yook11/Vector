"""Article completion repository — Stage 2 の永続化境界。

``pending_html_articles`` は補完待ち記事の作業テーブルだが、application service に
queue の状態モデルを漏らさない。Repository は「処理資格を満たす pending だけを
物体化する」「claim / sweep / retry 状態遷移を DB に反映する」までを担う。

他 Stage の ``ExtractionRepository`` / ``AssessmentRepository`` /
``EmbeddingRepository`` と同じく、SQLAlchemy Core/ORM の式で DB 操作を表現し、
呼び出し側が transaction の commit 境界を握る。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.incomplete_article import IncompleteArticle
from app.collection.persistence.article_store import ArticleStore
from app.collection.persistence.staged_attributes import StagedArticleAttributes
from app.models.pending_html_article import PendingHtmlArticle as PendingHtmlArticleORM
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


@dataclass(frozen=True, slots=True)
class CompletionPersistResult:
    """補完成功の永続化結果。

    ``pending_deleted=False`` は、service がロードした attempt が既に失効しており
    repository が pending 行に触れなかったことを表す。その場合 article insert は
    実行しない。
    """

    article_id: int | None
    pending_deleted: bool


class ArticleCompletionRepository:
    """Stage 2 completion に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_load_for_completion(
        self, pending_id: int
    ) -> ReadyForArticleCompletion | None:
        """``ReadyForArticleCompletion.try_advance_from`` 用ロード。

        ``status='running'`` の行だけを ``ReadyForArticleCompletion`` として
        物体化する。未 claim / sweep 済 / close 済 / delete 済の id はすべて
        ``None`` として扱い、Task は no-op で抜ける。
        """
        stmt = (
            select(
                PendingHtmlArticleORM.id,
                PendingHtmlArticleORM.url,
                PendingHtmlArticleORM.source_id,
                PendingHtmlArticleORM.staged_attributes,
                PendingHtmlArticleORM.attempt_count,
            )
            .where(
                PendingHtmlArticleORM.id == pending_id,
                PendingHtmlArticleORM.status == "running",
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None

        staged = StagedArticleAttributes.model_validate(row.staged_attributes)
        # ORM 列は SafeUrl 表現で読み出されるが、DB 上の値は INSERT 時の
        # canonical 値なので冪等に再構築できる。
        canonical_url = CanonicalArticleUrl(row.url.root)
        return ReadyForArticleCompletion(
            pending_id=row.id,
            source_id=row.source_id,
            attempt_count=row.attempt_count,
            incomplete_article=IncompleteArticle(
                title=staged.title,
                source_id=row.source_id,
                source_url=canonical_url,
                published_at_hint=staged.published_at_hint,
                prefer_html_title=staged.prefer_html_title,
            ),
        )

    async def claim_ready_batch(
        self,
        *,
        limit: int,
        now: datetime,
        leased_until: datetime,
    ) -> list[int]:
        """ready な open pending を claim し、claim できた id を返す。

        dispatcher が lease policy (``now`` / ``leased_until`` / ``limit``) を決め、
        repository は DB 更新だけを行う。``FOR UPDATE SKIP LOCKED`` で並行
        dispatcher が同じ行を二重 claim しない。
        """
        if limit <= 0:
            return []

        select_stmt = (
            select(PendingHtmlArticleORM.id)
            .where(
                PendingHtmlArticleORM.status == "open",
                PendingHtmlArticleORM.ready_at <= now,
            )
            .order_by(PendingHtmlArticleORM.ready_at, PendingHtmlArticleORM.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        ids = list((await self._session.execute(select_stmt)).scalars().all())
        if not ids:
            return []

        update_stmt = (
            update(PendingHtmlArticleORM)
            .where(
                PendingHtmlArticleORM.id.in_(ids),
                PendingHtmlArticleORM.status == "open",
            )
            .values(
                status="running",
                leased_until=leased_until,
                attempt_count=PendingHtmlArticleORM.attempt_count + 1,
                updated_at=now,
            )
            .returning(PendingHtmlArticleORM.id)
        )
        updated_ids = set((await self._session.execute(update_stmt)).scalars().all())
        return [pending_id for pending_id in ids if pending_id in updated_ids]

    async def sweep_expired_leases(self, *, now: datetime) -> int:
        """期限切れ lease の ``running`` 行を ``open`` に戻す。"""
        stmt = (
            update(PendingHtmlArticleORM)
            .where(
                PendingHtmlArticleORM.status == "running",
                PendingHtmlArticleORM.leased_until <= now,
            )
            .values(
                status="open",
                ready_at=now,
                leased_until=None,
                updated_at=now,
            )
            .returning(PendingHtmlArticleORM.id)
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
            update(PendingHtmlArticleORM)
            .where(
                PendingHtmlArticleORM.id == ready.pending_id,
                PendingHtmlArticleORM.status == "running",
                PendingHtmlArticleORM.attempt_count == ready.attempt_count,
            )
            .values(status="closed", leased_until=None, updated_at=now)
            .returning(PendingHtmlArticleORM.id)
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
            update(PendingHtmlArticleORM)
            .where(
                PendingHtmlArticleORM.id == ready.pending_id,
                PendingHtmlArticleORM.status == "running",
                PendingHtmlArticleORM.attempt_count == ready.attempt_count,
            )
            .values(
                status="open",
                ready_at=ready_at,
                leased_until=None,
                updated_at=now,
            )
            .returning(PendingHtmlArticleORM.id)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def persist_completed(
        self,
        ready: ReadyForArticleCompletion,
        advanced: AnalyzableArticle,
    ) -> CompletionPersistResult:
        """補完成功を永続化する。

        stale worker guard のため、まず ``pending_id`` + ``attempt_count`` で
        pending を DELETE する。DELETE できた場合だけ ``articles`` に INSERT し、
        ``source_url`` conflict は ``article_id=None`` として返す。
        """
        deleted = await self._delete_claimed(ready)
        if not deleted:
            return CompletionPersistResult(article_id=None, pending_deleted=False)

        article_id = await ArticleStore(self._session).save(advanced)
        return CompletionPersistResult(article_id=article_id, pending_deleted=True)

    async def _delete_claimed(self, ready: ReadyForArticleCompletion) -> bool:
        stmt = (
            delete(PendingHtmlArticleORM)
            .where(
                PendingHtmlArticleORM.id == ready.pending_id,
                PendingHtmlArticleORM.status == "running",
                PendingHtmlArticleORM.attempt_count == ready.attempt_count,
            )
            .returning(PendingHtmlArticleORM.id)
        )
        return (await self._session.execute(stmt)).first() is not None
