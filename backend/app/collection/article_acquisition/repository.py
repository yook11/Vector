"""Stage 1 (article_acquisition) の ``incomplete_articles`` 投入専用 writer。

commit は呼び出し側 (Service) が行う。本 writer は SQL 発行までで止まる。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.domain.observed_article import ObservedArticle
from app.models.incomplete_article import IncompleteArticle as IncompleteArticleORM


class IncompleteArticleRepository:
    """``incomplete_articles`` への Stage 1 投入 (``status='open'``)。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        observed: ObservedArticle,
        *,
        source_id: int,
        ready_at: datetime,
    ) -> int | None:
        """``ObservedArticle`` を ``status='open'`` で INSERT し、id を返す。

        UNIQUE(url) 違反 (race-loss) の場合は ``None`` を返す。commit は
        呼び出し側 (Service) が行う。
        """
        stmt = (
            pg_insert(IncompleteArticleORM)
            .values(
                url=observed.source_url,
                source_id=source_id,
                # identity を表層列に書く (spec ``Pending source identity
                # refactor.md`` #1/#7)。``observed.source_name`` は
                # ``Field(exclude=True)`` で JSONB から除外され、ここで
                # 表層列に焼かれる (倒立解消)。
                source_name=observed.source_name,
                status="open",
                staged_attributes=observed.to_staged_attributes(),
                ready_at=ready_at,
                leased_until=None,
                attempt_count=0,
            )
            .on_conflict_do_nothing()
            .returning(IncompleteArticleORM.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row.id if row is not None else None
