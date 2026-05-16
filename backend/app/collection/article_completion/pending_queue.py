"""Stage 2 (article_completion) の ``pending_html_articles`` lease キュー操作。

PR2.5-A の lease 方式キューに対する Stage 2 側操作 (claim / sweep / 状態遷移 /
読出 / 削除) を集約する。Stage 1 側の投入 (``status='open'`` INSERT) は
``source_fetch/pending_enqueue.py`` が担い、本キューとは相互 import しない
(1 テーブルを 2 工程から操作するが依存方向は分離)。共有する永続化フォーマット
``StagedArticleAttributes`` は中立な ``persistence/`` から取り込む。

責務:

- ``find_by_id``: ``ArticleCompletionService`` 入口で pending を SELECT (``url`` を
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
- commit は全て呼び出し側 (Service / cron task) が行う。本キューは SQL 発行
  までで止まる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.domain.incomplete_article import IncompleteArticle
from app.collection.persistence.staged_attributes import StagedArticleAttributes
from app.models.pending_html_article import PendingHtmlArticle as PendingHtmlArticleORM
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


@dataclass(frozen=True, slots=True)
class PendingHtmlRowMeta:
    """``pending_html_articles`` 行の lease / status / FK メタ情報。

    Domain (``IncompleteArticle``) と独立した「行の運用状態」を表す。cron poller
    の claim / sweep、``ArticleCompletionService`` の状態遷移はこの行メタを介する。
    """

    id: int
    source_id: int
    status: str
    ready_at: datetime | None
    leased_until: datetime | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class PendingHtmlContext:
    """``find_by_id`` の戻り値: pending 1 行の合成 view。

    Domain (``IncompleteArticle``) と行メタ (``PendingHtmlRowMeta``) を明示的に
    分離する。``ArticleCompletionService`` は ``ctx.incomplete_article`` で Domain
    操作、``ctx.row_meta`` で lease / status 判定を行う (責務が型レベルで分離)。
    """

    incomplete_article: IncompleteArticle
    row_meta: PendingHtmlRowMeta


class PendingHtmlQueue:
    """``pending_html_articles`` への Stage 2 claim/sweep/状態遷移操作。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, pending_id: int) -> PendingHtmlContext | None:
        """``pending_id`` 1 件を取得する。

        ``ArticleCompletionService.execute`` の入口で 1 SELECT で必要情報を全部取る。
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
        staged = StagedArticleAttributes.model_validate(row.staged_attributes)
        # ORM 列は SafeUrl 表現で読み出されるが、DB 上の値は INSERT 時の
        # canonical 値 (`create` で構造保証済) なので冪等に再構築できる。
        canonical_url = CanonicalArticleUrl(row.url.root)
        return PendingHtmlContext(
            incomplete_article=IncompleteArticle(
                title=staged.title,
                source_id=row.source_id,
                source_url=canonical_url,
                published_at_hint=staged.published_at_hint,
                prefer_html_title=staged.prefer_html_title,
            ),
            row_meta=PendingHtmlRowMeta(
                id=row.id,
                source_id=row.source_id,
                status=row.status,
                ready_at=row.ready_at,
                leased_until=row.leased_until,
                attempt_count=row.attempt_count,
            ),
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
