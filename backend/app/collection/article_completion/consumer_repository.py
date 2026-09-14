"""新経路の補完に必要な未完成記事の記録と、非closed行の状態遷移を扱う。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import String, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.sources.source_name import SourceName
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.incomplete_article import IncompleteArticle


@dataclass(frozen=True, slots=True)
class RecordedIncompleteArticle:
    """実行権や試行番号を含まない、未完成記事の保存済み記録。"""

    incomplete_article_id: int
    status: str
    source_id: int
    source_name: SourceName
    source_url: str
    observed_article: dict[str, Any]


class ArticleCompletionConsumerRepository:
    """同じ未完成行の削除と終了更新を競合させ、先行確定を守る。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(
        self, incomplete_article_id: int
    ) -> RecordedIncompleteArticle | None:
        row = (
            (
                await self._session.execute(
                    select(
                        IncompleteArticle.id.label("incomplete_article_id"),
                        IncompleteArticle.status,
                        IncompleteArticle.source_id,
                        IncompleteArticle.source_name,
                        IncompleteArticle.url.cast(String).label("source_url"),
                        IncompleteArticle.observed_article,
                    ).where(IncompleteArticle.id == incomplete_article_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return RecordedIncompleteArticle(**row) if row is not None else None

    async def has_completed_article(self, source_url: str) -> bool:
        stmt = select(AnalyzableArticleRecord.id).where(
            AnalyzableArticleRecord.source_url == source_url
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def delete_nonclosed(self, incomplete_article_id: int) -> bool:
        stmt = (
            delete(IncompleteArticle)
            .where(
                IncompleteArticle.id == incomplete_article_id,
                IncompleteArticle.status != "closed",
            )
            .returning(IncompleteArticle.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def close_nonclosed(
        self, incomplete_article_id: int, *, now: datetime
    ) -> bool:
        stmt = (
            update(IncompleteArticle)
            .where(
                IncompleteArticle.id == incomplete_article_id,
                IncompleteArticle.status != "closed",
            )
            .values(status="closed", leased_until=None, updated_at=now)
            .returning(IncompleteArticle.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None
