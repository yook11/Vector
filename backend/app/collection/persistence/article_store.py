"""``articles`` 行の書込と重複判定 — 両工程共有の小 Repository。

読み側 (``app/repositories/articles.py::ArticleRepository``) と分けるため
書込側を ``ArticleStore`` と命名する。
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.models.article import Article as ArticleORM


class ArticleStore:
    """``articles`` 行の書込と重複判定 (両工程共有)。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, ready: AnalyzableArticle) -> int | None:
        """``AnalyzableArticle`` を ``articles`` に INSERT し新規 ``id`` を返す。

        ``ON CONFLICT DO NOTHING`` で並行レース / 既知 URL を吸収し、新規行が
        作れなかった場合は ``None`` を返す。``source_url`` は ``CanonicalArticleUrl``
        なので再正規化不要。commit は呼び出し側 (Service) が行う。
        """
        stmt = (
            pg_insert(ArticleORM)
            .values(
                source_id=ready.source_id,
                source_url=ready.source_url,
                original_title=ready.title,
                original_content=ready.body,
                published_at=ready.published_at.value,
            )
            .on_conflict_do_nothing()
            .returning(ArticleORM.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row.id if row is not None else None

    async def exists_by_source_url(self, source_url: CanonicalArticleUrl) -> bool:
        """``source_url`` を持つ ``articles`` 行が既に存在するかを軽量確認する。

        補完待ち獲得の pre-check 用 (feed 再露出時に既知 URL の pending 化を回避し、
        HTML fetch の反復コストを抑える)。ロックではない idempotency で、同 tick
        race は ``save`` 側の ``ON CONFLICT DO NOTHING`` が吸収する。
        """
        stmt = select(ArticleORM.id).where(ArticleORM.source_url == source_url).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None
