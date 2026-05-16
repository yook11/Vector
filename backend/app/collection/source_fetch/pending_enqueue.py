"""Stage 1 (source_fetch) の ``pending_html_articles`` 投入専用 writer。

補完待ち獲得経路の 1 段目 (``ArticleAcquisitionService``) が、本文 HTML 取得を
要する記事を ``pending_html_articles`` に ``status='open'`` で 1 件積む。Stage 2
側の claim / sweep / 状態遷移は ``article_completion/repository.py`` が担い、
本 writer とは相互 import しない (1 テーブルを 2 工程から操作するが、依存方向は
分離する)。共有する永続化フォーマット ``StagedArticleAttributes`` は中立な
``persistence/`` から取り込む。

責務:

- ``enqueue``: Pattern H 振り分けで ``IncompleteArticle`` を 1 件 INSERT。
  ``UNIQUE(url)`` 違反は ``None`` 戻し (同 tick race 敗北)。``IncompleteArticle``
  を直接受け、Repo 側で永続化フォーマット (``StagedArticleAttributes`` JSONB) に
  詰める (姉妹 ``ArticleStore.save`` との対称)。

commit は呼び出し側 (Service) が行う。本 writer は SQL 発行までで止まる。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.domain.incomplete_article import IncompleteArticle
from app.collection.persistence.staged_attributes import StagedArticleAttributes
from app.models.pending_html_article import PendingHtmlArticle as PendingHtmlArticleORM


class PendingHtmlEnqueue:
    """``pending_html_articles`` への Stage 1 投入 (``status='open'``)。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        incomplete: IncompleteArticle,
        *,
        ready_at: datetime,
    ) -> int | None:
        """``IncompleteArticle`` を ``pending_html_articles`` に
        ``status='open'`` で INSERT し、id を返す。

        Aggregate (``IncompleteArticle``) を Repo が直接受け、永続化フォーマット
        (``StagedArticleAttributes`` JSONB) への詰替えを Repo 内で完結させる。
        UNIQUE(url) 違反 (race-loss) の場合は ``None`` を返す。``source_url`` の
        canonical 性は ``CanonicalArticleUrl`` 型で構造保証されているため
        Repository での後付け正規化は不要。ORM 列は ``SafeUrl`` 表現だが
        ``SafeUrlType.process_bind_param`` が ``CanonicalArticleUrl`` を透過
        bind する。commit は呼び出し側 (Service) が行う。
        """
        stmt = (
            pg_insert(PendingHtmlArticleORM)
            .values(
                url=incomplete.source_url,
                source_id=incomplete.source_id,
                status="open",
                staged_attributes=StagedArticleAttributes(
                    title=incomplete.title,
                    published_at_hint=incomplete.published_at_hint,
                    prefer_html_title=incomplete.prefer_html_title,
                ).model_dump(mode="json"),
                ready_at=ready_at,
                leased_until=None,
                attempt_count=0,
            )
            .on_conflict_do_nothing()
            .returning(PendingHtmlArticleORM.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row.id if row is not None else None
