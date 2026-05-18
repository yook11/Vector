"""``source_id`` / ``source_name`` → ``SourceCompletionProfile`` 解決 seam。

``source_name`` は新 pending 行のみ持つ。欠落する旧行は ``source_id`` から
``news_sources.name`` を DB 解決する。
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.strategy import SOURCES
from app.models.news_source import NewsSource as NewsSourceORM
from app.shared.value_objects.source_name import SourceName


class CompletionProfileResolver(Protocol):
    """補完方針 / ソース名の解決契約 (repository が依存する seam)。"""

    async def resolve(
        self, *, source_id: int, source_name: SourceName | None
    ) -> SourceCompletionProfile: ...

    async def resolve_name(self, *, source_id: int) -> SourceName: ...


class RegistryCompletionProfileResolver:
    """``SOURCES`` 引き + ``source_id→name`` DB フォールバックの具象。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_name(self, *, source_id: int) -> SourceName:
        """``news_sources.name`` を引く (不在は ``ValueError``)。"""
        stmt = select(NewsSourceORM.name).where(NewsSourceORM.id == source_id)
        row = (await self._session.execute(stmt)).first()
        if row is None:
            msg = f"news_source not found: id={source_id}"
            raise ValueError(msg)
        return row.name

    async def resolve(
        self, *, source_id: int, source_name: SourceName | None
    ) -> SourceCompletionProfile:
        """補完方針を解決する。``source_name`` 欠落 (旧行) は
        ``source_id`` から DB 解決する。未登録ソースは ``DEFAULT_PROFILE``。
        """
        name = (
            source_name
            if source_name is not None
            else await self.resolve_name(source_id=source_id)
        )
        source = SOURCES.get(SourceName(str(name)))
        return source.completion_profile if source is not None else DEFAULT_PROFILE
