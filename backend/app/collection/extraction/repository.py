"""Extraction リポジトリ — Article の永続化と読み出し。

責務:

- ``ArticleRepository.save_via_article_url``: ``ArticleDraft`` を
  ``articles`` 行に INSERT し、DB が採番した identity
  (``PersistedArticleId``) を返す。並行レースは
  ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` で構造的に解消し、
  既に他ワーカーが書き込み済みなら ``None`` を返す。
- ``ArticleRepository.find_by_article_url_id``: 並行レース敗北時の
  読み戻し用に Article Entity を取得する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.extraction.domain import Article, PublishedAt
from app.collection.extraction.domain.article import ArticleDraft
from app.models.article import Article as ArticleORM
from app.shared.value_objects.safe_url import SafeUrl


@dataclass(frozen=True, slots=True)
class PersistedArticleId:
    """``ArticleRepository.save_via_article_url`` が DB から受け取った identity 値。

    Service はこの値と元の ``ArticleDraft`` を ``Article.from_draft_via_article_url``
    に渡して記録済み Entity を組み立てる。
    """

    id: int
    created_at: datetime


def _article_from_orm(orm: ArticleORM) -> Article:
    """``ArticleORM`` から ``Article`` Entity への共通変換ヘルパ。

    Entity の不変条件 (id 正、``article_url_id`` が positive) は
    ``Article.__post_init__`` が defense-in-depth として再検証する。
    """
    published_at = (
        PublishedAt(orm.published_at) if orm.published_at is not None else None
    )
    return Article(
        id=orm.id,
        article_url_id=orm.article_url_id,
        title=orm.original_title,
        body=orm.original_content,
        published_at=published_at,
        created_at=orm.created_at,
    )


class ArticleRepository:
    """``Article`` 行の永続化と読み出し。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_via_article_url(
        self,
        draft: ArticleDraft,
        *,
        article_url_id: int,
        source_id: int,
        source_url: SafeUrl,
    ) -> PersistedArticleId | None:
        """``article_url_id`` 主軸で ``articles`` 行を INSERT する。

        ``ON CONFLICT DO NOTHING`` (制約ターゲット指定なし) で並行レース時の
        全 unique 違反を吸収する。``articles`` には複数の UNIQUE
        (``uq_articles_article_url_id`` / ``uq_articles_source_url``) が
        張られるため、どの conflict も「他者が先に書込済み」と等価扱い。
        ``None`` を受けた Service は ``find_by_article_url_id`` で読み戻して
        合流させる。

        ``source_id`` / ``source_url`` は同一トランザクション内で caller が
        既知の値として渡す。commit は呼び出し側 (Service) が行う。
        """
        stmt = (
            pg_insert(ArticleORM)
            .values(
                article_url_id=article_url_id,
                source_id=source_id,
                source_url=source_url,
                original_title=draft.title,
                original_content=draft.body,
                published_at=(
                    draft.published_at.value if draft.published_at is not None else None
                ),
            )
            .on_conflict_do_nothing()
            .returning(ArticleORM.id, ArticleORM.created_at)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return PersistedArticleId(id=row.id, created_at=row.created_at)

    async def find_by_article_url_id(self, article_url_id: int) -> Article | None:
        """``article_url_id`` から既存 Article を Entity として取得する。

        Stage 2 の race-loss (``ConflictLost``) 検出時の読み戻しに使う。
        """
        stmt = select(ArticleORM).where(ArticleORM.article_url_id == article_url_id)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return _article_from_orm(orm) if orm is not None else None
