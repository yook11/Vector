"""依頼されたソースの現状態を確認し、既存の記事取得を実行する。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.collection.article_acquisition.failure_handling import (
    ArticleAcquisitionFailureHandler,
)
from app.collection.article_acquisition.service import ArticleAcquisitionService
from app.collection.article_acquisition.source_resolution import (
    AcquisitionNotRequired,
    resolve_acquisition_source,
)
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.sources.acquisition_request import SourceAcquisitionRequest
from app.db.session import SessionFactory


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
        self._failure_handler = ArticleAcquisitionFailureHandler(session_factory)

    async def consume(self, request: SourceAcquisitionRequest) -> AcquisitionResult:
        source = await resolve_acquisition_source(
            source_id=request.source_id, session_factory=self._session_factory
        )
        if isinstance(source, AcquisitionNotRequired):
            return AcquisitionResult(source.reason)
        service = ArticleAcquisitionService(
            self._session_factory, source, self._tools_factory
        )
        try:
            ids = await service.execute(source_id=request.source_id)
        except Exception as exc:
            await self._failure_handler.record_source_failure(
                source_id=request.source_id,
                source_name=str(source.name),
                exc=exc,
            )
            raise
        return AcquisitionResult("acquired", len(ids))
