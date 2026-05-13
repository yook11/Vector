"""``pending_html_articles`` (HTML 取得待ちキュー) 向け Repository。

PR2.5-A の lease 方式キューに対する全 CRUD + claim/sweep 操作を集約する。

責務:

- ``create``: Pattern H 振り分け entry を 1 件 INSERT。``UNIQUE(url)`` 違反は
  ``None`` 戻し (同 tick race 敗北)。
- ``find_by_id``: ``ContentFetchService`` 入口で pending を SELECT (``url`` を
  pending 行から直接取得、JOIN 不要)。
- ``claim_batch``: cron poller が ``status='open' AND ready_at <= NOW()`` の行を
  ``FOR UPDATE SKIP LOCKED`` で原子的に claim、``status='running'`` +
  ``leased_until=NOW()+lease_minutes`` + ``attempt_count++`` を 1 文で更新。
- ``sweep_expired``: 死んだ worker の lease 切れ ``running`` を ``open`` に戻す。
- ``mark_terminal`` / ``mark_will_retry`` / ``mark_exhausted``: Stage 2 失敗系の
  状態遷移。caller (Service) が outcome に応じて使い分ける。
- ``delete_one``: 永続化成功時に ``articles`` INSERT と同 tx で pending を消す。

設計上の決定:

- ``claim_batch`` / ``sweep_expired`` は raw SQL (``text``) で書く。SQLAlchemy
  Core で ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`` を
  組み立てるより SQL の方が読み手に意図が伝わりやすい。
- ``mark_terminal`` と ``mark_exhausted`` は DB 上は同じ effect (status='closed')
  だが、Service 側で audit ``outcome_code`` を区別したいので別 method として
  維持する (DRY 共有しない)。
- commit は全て呼び出し側 (Service / cron task) が行う。Repository は SQL 発行
  までで止まる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.ingestion.staged_attributes import StagedArticleAttributes
from app.models.pending_html_article import PendingHtmlArticle as PendingHtmlArticleORM
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


@dataclass(frozen=True, slots=True)
class PendingHtmlContext:
    """``find_by_id`` の戻り値: pending 1 行の view。

    ``ContentFetchService`` が必要とする全情報を 1 SELECT で持ち回るための
    structured tuple。``staged_attributes`` は JSONB を ``StagedArticleAttributes``
    に再構築済 (PublishedAt ISO ↔ datetime 変換を Repository 層で吸収する)。

    ``url`` は ``CanonicalArticleUrl`` で canonical 性を構造保証する
    (``articles.source_url`` と一致する形)。
    """

    id: int
    source_id: int
    status: str
    staged_attributes: StagedArticleAttributes
    ready_at: datetime | None
    leased_until: datetime | None
    attempt_count: int
    url: CanonicalArticleUrl


class PendingHtmlArticleRepository:
    """``pending_html_articles`` への CRUD + claim/sweep 操作。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        url: CanonicalArticleUrl,
        source_id: int,
        staged_attributes: StagedArticleAttributes,
        ready_at: datetime,
    ) -> int | None:
        """新規 pending を ``status='open'`` で INSERT し、id を返す。

        UNIQUE 違反 (race-loss) の場合は ``None`` を返す。``url`` の UNIQUE は
        canonical 値で効き、``CanonicalArticleUrl`` 型で canonical 性は構造保証
        されているため caller / Repository での後付け正規化は不要。ORM 列は
        ``SafeUrl`` 表現だが ``SafeUrlType.process_bind_param`` が
        ``CanonicalArticleUrl`` を透過 bind する。
        """
        stmt = (
            pg_insert(PendingHtmlArticleORM)
            .values(
                url=url,
                source_id=source_id,
                status="open",
                staged_attributes=staged_attributes.model_dump(mode="json"),
                ready_at=ready_at,
                leased_until=None,
                attempt_count=0,
            )
            .on_conflict_do_nothing()
            .returning(PendingHtmlArticleORM.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row.id if row is not None else None

    async def find_by_id(self, pending_id: int) -> PendingHtmlContext | None:
        """``pending_id`` 1 件を取得する。

        ``ContentFetchService.execute`` の入口で 1 SELECT で必要情報を全部取る。
        見つからない場合は ``None`` (重複配送 / DELETE 済の静かな exit に使う)。
        """
        stmt = select(
            PendingHtmlArticleORM.id,
            PendingHtmlArticleORM.url,
            PendingHtmlArticleORM.source_id,
            PendingHtmlArticleORM.status,
            PendingHtmlArticleORM.staged_attributes,
            PendingHtmlArticleORM.ready_at,
            PendingHtmlArticleORM.leased_until,
            PendingHtmlArticleORM.attempt_count,
        ).where(PendingHtmlArticleORM.id == pending_id)
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return PendingHtmlContext(
            id=row.id,
            source_id=row.source_id,
            status=row.status,
            staged_attributes=StagedArticleAttributes.model_validate(
                row.staged_attributes
            ),
            ready_at=row.ready_at,
            leased_until=row.leased_until,
            attempt_count=row.attempt_count,
            # ORM 列は SafeUrl 表現で読み出されるが、DB 上の値は INSERT 時の
            # canonical 値 (`create` で構造保証済) なので冪等に再構築できる。
            url=CanonicalArticleUrl(row.url.root),
        )

    async def claim_batch(self, *, limit: int, lease_minutes: int) -> list[int]:
        """``ready_at <= NOW()`` の ``open`` 行を最大 ``limit`` 件 claim する。

        ``FOR UPDATE SKIP LOCKED`` で並行 cron worker が同じ行を二重 claim
        しない。原子的に ``status='running'`` + ``leased_until=NOW()+N min`` +
        ``attempt_count++`` に更新し、claim できた id 列を返す。

        ``order_by ready_at`` は古い ready 順 (= 待たされている順) で公平性を担保。

        commit は呼び出し側 (cron poller) が行う。
        """
        sql = text(
            """
            UPDATE pending_html_articles
               SET status        = 'running',
                   leased_until  = NOW() + (:lease_minutes * INTERVAL '1 minute'),
                   attempt_count = attempt_count + 1,
                   updated_at    = NOW()
             WHERE id IN (
                 SELECT id
                   FROM pending_html_articles
                  WHERE status = 'open'
                    AND ready_at <= NOW()
                  ORDER BY ready_at
                    FOR UPDATE SKIP LOCKED
                  LIMIT :limit
             )
            RETURNING id
            """
        )
        rows = (
            await self._session.execute(
                sql, {"limit": limit, "lease_minutes": lease_minutes}
            )
        ).all()
        return [row.id for row in rows]

    async def sweep_expired(self) -> int:
        """死んだ worker の lease 切れ ``running`` を ``open`` に戻す。

        ``status='running' AND leased_until <= NOW()`` の行を一括で
        ``status='open'`` + ``ready_at=NOW()`` (即時 picking 候補) +
        ``leased_until=NULL`` に戻す。

        Returns: 戻した行数。

        commit は呼び出し側 (cron poller) が行う。
        """
        sql = text(
            """
            UPDATE pending_html_articles
               SET status       = 'open',
                   ready_at     = NOW(),
                   leased_until = NULL,
                   updated_at   = NOW()
             WHERE status = 'running'
               AND leased_until <= NOW()
            RETURNING id
            """
        )
        rows = (await self._session.execute(sql)).all()
        return len(rows)

    async def mark_terminal(self, pending_id: int) -> None:
        """永続失敗 (404 / 410 / extraction_empty 等) で ``closed`` に閉じる。

        ``status='closed'`` + ``leased_until=NULL``。``ready_at`` は触らない
        (CHECK 上 closed なら任意)。
        """
        await self._close(pending_id)

    async def mark_exhausted(self, pending_id: int) -> None:
        """retry 予算 (per-policy max_attempts) を使い切って ``closed`` に閉じる。

        DB 上の effect は ``mark_terminal`` と同じだが、Service 側で audit に出す
        ``outcome_code`` (``dropped_transient``) が違うため method を分けている。
        """
        await self._close(pending_id)

    async def mark_will_retry(self, pending_id: int, *, ready_at: datetime) -> None:
        """一時失敗で ``open`` に戻し、次回 ``ready_at`` まで backoff する。

        ``status='open'`` + ``ready_at=次回時刻`` + ``leased_until=NULL``。
        """
        sql = text(
            """
            UPDATE pending_html_articles
               SET status       = 'open',
                   ready_at     = :ready_at,
                   leased_until = NULL,
                   updated_at   = NOW()
             WHERE id = :id
            """
        )
        await self._session.execute(sql, {"id": pending_id, "ready_at": ready_at})

    async def delete_one(self, pending_id: int) -> None:
        """成功時に pending 行を削除する (``articles`` INSERT と同 tx で実行)."""
        sql = text("DELETE FROM pending_html_articles WHERE id = :id")
        await self._session.execute(sql, {"id": pending_id})

    async def _close(self, pending_id: int) -> None:
        sql = text(
            """
            UPDATE pending_html_articles
               SET status       = 'closed',
                   leased_until = NULL,
                   updated_at   = NOW()
             WHERE id = :id
            """
        )
        await self._session.execute(sql, {"id": pending_id})
