"""収集対象の選定と取得前確認に必要なソースの保存状態を読み取る。"""

from dataclasses import dataclass

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news_source import NewsSource


@dataclass(frozen=True, slots=True)
class RecordedSource:
    """登録定義との照合前の名前と、DB上の有効状態を保持する。"""

    id: int
    raw_name: str
    is_active: bool


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[RecordedSource]:
        raw_name = cast(NewsSource.name, String).label("raw_name")
        rows = (
            await self._session.execute(
                select(NewsSource.id, raw_name, NewsSource.is_active)
                .where(NewsSource.is_active.is_(True))
                .order_by(raw_name)
            )
        ).all()
        return [RecordedSource(row.id, row.raw_name, row.is_active) for row in rows]

    async def get_by_id(self, source_id: int) -> RecordedSource | None:
        row = (
            await self._session.execute(
                select(
                    NewsSource.id,
                    cast(NewsSource.name, String).label("raw_name"),
                    NewsSource.is_active,
                ).where(NewsSource.id == source_id)
            )
        ).one_or_none()
        return RecordedSource(row.id, row.raw_name, row.is_active) if row else None
