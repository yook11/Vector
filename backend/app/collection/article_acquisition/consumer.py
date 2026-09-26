"""依頼されたソースの現状態を確認し、既存の記事取得を実行する。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.collection.article_acquisition.consumer_failure_classification import (
    AcquisitionFailureDecision,
    classify_acquisition_failure,
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


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    result: Literal["acquired", "inactive", "missing"]
    created_count: int = 0


@dataclass(frozen=True, slots=True)
class AcquisitionFailed:
    """取得の元例外と、再配信するか断念するかの判断を保持する。"""

    error: Exception
    decision: AcquisitionFailureDecision


class ArticleAcquisitionConsumer:
    def __init__(
        self, session_factory: SessionFactory, tools_factory: Callable[[], ReaderTools]
    ) -> None:
        self._session_factory = session_factory
        self._tools_factory = tools_factory
        self._failure_recorder = ArticleAcquisitionFailureRecorder(session_factory)

    async def consume(
        self, request: SourceAcquisitionRequest
    ) -> AcquisitionResult | AcquisitionFailed:
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
            decision = classify_acquisition_failure(exc, now=datetime.now(UTC))
            await self._failure_recorder.record_source_failure(
                source_id=request.source_id,
                source_name=str(source.name),
                exc=exc,
                decision=decision,
            )
            return AcquisitionFailed(error=exc, decision=decision)
        return AcquisitionResult("acquired", len(ids))
