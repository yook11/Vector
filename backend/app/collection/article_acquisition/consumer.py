"""依頼されたソースの現状態を確認し、既存の記事取得を実行する。"""

from collections.abc import Callable
from datetime import UTC, datetime

from app.collection.article_acquisition.consumer_failure_classification import (
    classify_acquisition_failure,
)
from app.collection.article_acquisition.consumer_result import (
    AcquisitionSucceeded,
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.collection.article_acquisition.failure_recording import (
    ArticleAcquisitionFailureRecorder,
)
from app.collection.article_acquisition.service import ArticleAcquisitionService
from app.collection.article_acquisition.source_resolution import (
    AcquisitionNotRequired,
    resolve_acquisition_source,
)
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.sources.acquisition_request import SourceAcquisitionRequest
from app.db.session import SessionFactory


class ArticleAcquisitionConsumer:
    def __init__(
        self, session_factory: SessionFactory, tools_factory: Callable[[], ReaderTools]
    ) -> None:
        self._session_factory = session_factory
        self._tools_factory = tools_factory
        self._failure_recorder = ArticleAcquisitionFailureRecorder(session_factory)

    async def consume(
        self, request: SourceAcquisitionRequest
    ) -> AcquisitionSucceeded | RetryAcquisition | NoRetryAcquisition:
        source = await resolve_acquisition_source(
            source_id=request.source_id, session_factory=self._session_factory
        )
        if isinstance(source, AcquisitionNotRequired):
            return NoRetryAcquisition(cause=source.reason)
        service = ArticleAcquisitionService(
            self._session_factory, source, self._tools_factory
        )
        try:
            ids = await service.execute(source_id=request.source_id)
        except Exception as exc:
            failure = classify_acquisition_failure(exc, now=datetime.now(UTC))
            await self._failure_recorder.record_source_failure(
                source_id=request.source_id,
                source_name=str(source.name),
                failure=failure,
            )
            return failure
        return AcquisitionSucceeded(len(ids))
