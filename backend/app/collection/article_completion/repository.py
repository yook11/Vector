"""補完の永続化境界。

``incomplete_articles`` は補完待ち記事の作業テーブルだが、application service
に queue の状態モデルを漏らさない。Repository は処理資格を満たす pending の
物体化と、claim / sweep / retry 状態遷移の DB 反映までを担う。commit 境界は
呼び出し側が握る。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.article_acquisition.strategy import SOURCES
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.persistence.article_store import ArticleStore
from app.models.incomplete_article import IncompleteArticle as IncompleteArticleORM


@dataclass(frozen=True, slots=True)
class CompletionSucceeded:
    """正規所有者として ``articles`` に INSERT 成功。"""

    article_id: int


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

    async def try_load_for_completion(
        self, pending_id: int
    ) -> ReadyForArticleCompletion | None:
        """``ReadyForArticleCompletion`` を構築するためのロード。

        ``status='running'`` の行だけを物体化する。未 claim / sweep 済 /
        close 済 / delete 済の id はすべて ``None`` として扱う。

        identity 注入: ``url`` / ``source_name`` 列が authoritative
        (composite FK + NOT NULL で構造保証されており、Stage 1 writer の
        ``Field(exclude=True)`` 契約と対称)。JSONB は identity を含まないため、
        ``model_validate`` 前に表層列の値を raw に上書き注入する。

        profile 解決は ``SOURCES[source_name]`` 直叩き。registry 未登録
        source は ``KeyError`` で上位に伝播する (drift fallback の隠蔽は
        ``[[feedback_failure_visibility]]`` により禁止)。
        """
        stmt = (
            select(IncompleteArticleORM)
            .where(
                IncompleteArticleORM.id == pending_id,
                IncompleteArticleORM.status == "running",
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None

        raw = dict(row.staged_attributes or {})
        # 表層列が authoritative。JSONB は ``Field(exclude=True)`` で identity
        # キーを持たないため、表層列の値を raw に上書き注入する。書き込むキーは
        # ``ObservedArticle`` field の alias 規約に追従:
        #   source_name は alias="sourceName" → "sourceName" で書く
        #   source_url は alias 未指定 (exclude=True のみ) → field name
        #     "source_url" で書く。
        raw["sourceName"] = str(row.source_name)
        raw["source_url"] = str(row.url)
        observed = ObservedArticle.model_validate(raw)
        # ORM 列は SafeUrl 表現で読み出されるが、DB 上の値は INSERT 時の
        # canonical 値なので冪等に再構築できる。CanonicalArticleUrl の再検証は
        # per-row で 1 回走る (cost は微小、SafeUrl → CanonicalArticleUrl の
        # column type 昇格は別 PR の射程)。
        source_url = CanonicalArticleUrl(str(row.url))
        profile = SOURCES[observed.source_name].completion_policy
        return ReadyForArticleCompletion(
            pending_id=row.id,
            source_id=row.source_id,
            attempt_count=row.attempt_count,
            observed=observed,
            profile=profile,
            source_url=source_url,
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
        """補完成功を永続化し、3 つの outcome を名前付きで返す。

        stale worker guard のため、まず ``pending_id`` + ``attempt_count`` で
        pending を DELETE する。DELETE が 0 行なら ``CompletionSuperseded``
        (attempt 失効) で短絡し ``articles`` には触れない。DELETE できた場合だけ
        INSERT し、``source_url`` conflict は ``CompletionUrlConflict``、成功は
        ``CompletionSucceeded`` として返す。
        """
        if not await self._delete_claimed(ready):
            return CompletionSuperseded()

        article_id = await ArticleStore(self._session).save(advanced)
        if article_id is None:
            return CompletionUrlConflict()
        return CompletionSucceeded(article_id=article_id)

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
