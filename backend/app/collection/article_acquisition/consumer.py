"""依頼されたソースの現状態を確認し、既存の記事取得を実行する。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import String, cast, select

from app.collection.article_acquisition.service import ArticleAcquisitionService
from app.collection.article_acquisition.strategy import SOURCES
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.sources.acquisition_request import SourceAcquisitionRequest
from app.collection.sources.source_name import SourceName
from app.db.session import SessionFactory
from app.models.news_source import NewsSource


class AcquisitionSourceInvalidError(Exception):
    """有効なソースを登録済みの取得定義へ解決できない。"""


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    result: Literal["acquired", "inactive", "missing"]
    created_count: int = 0


class ArticleAcquisitionConsumer:
    def __init__(
        self, session_factory: SessionFactory, tools_factory: Callable[[], ReaderTools]
    ) -> None:
        self._session_factory = session_factory
        self._tools_factory = tools_factory

    async def consume(self, request: SourceAcquisitionRequest) -> AcquisitionResult:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        cast(NewsSource.name, String).label("name"),
                        NewsSource.is_active,
                    ).where(NewsSource.id == request.source_id)
                )
            ).one_or_none()
        if row is None:
            return AcquisitionResult("missing")
        if not row.is_active:
            return AcquisitionResult("inactive")
        try:
            source = SOURCES.get(SourceName(row.name))
        except ValueError:
            source = None
        if source is None:
            raise AcquisitionSourceInvalidError("source_not_registered")
        ids = await ArticleAcquisitionService(
            self._session_factory, source, self._tools_factory
        ).execute(source_id=request.source_id)
        return AcquisitionResult("acquired", len(ids))
