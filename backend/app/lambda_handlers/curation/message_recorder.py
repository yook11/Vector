"""Curationのメッセージ処理の開始と終わり方を、AI分析のログ方針の形で記録する。"""

from structlog.typing import FilteringBoundLogger

from app.analysis.curation.consumer_failure_classification import (
    NoRetryCuration,
    RetryCuration,
)
from app.analysis.curation.domain.ready import CurationReadyBuildRejected
from app.analysis.curation.service import CurationCompletion
from app.collection.events import AnalyzableEventInvalidError
from app.lambda_handlers.event_reader import EventReadFailed
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError


class CurationMessageRecorder:
    """メッセージの終わり方ごとに、終端ログのイベント名・レベル・項目を決める。"""

    def __init__(self, logger: FilteringBoundLogger) -> None:
        self._logger = logger

    def record_started(self) -> None:
        self._logger.info("curation_message_processing_started")

    def record_read_failed(
        self, read_failed: EventReadFailed, *, duration_ms: float
    ) -> None:
        logger = self._logger.bind(
            duration_ms=duration_ms, message_disposition="batch_item_failure"
        )
        match read_failed.error:
            case SqsInputError() | SqsMessageJsonInvalidError() as error:
                logger.warning(
                    "curation_message_processing_failed",
                    operation="parse_message",
                    exc_info=error,
                )
            case AnalyzableEventInvalidError() as error:
                logger.warning(
                    "curation_message_processing_failed",
                    operation="validate_event",
                    exc_info=error,
                )
            case error:
                logger.error(
                    "curation_message_processing_failed",
                    operation="parse_message",
                    exc_info=error,
                )

    def record_consumed(
        self,
        consume_result: CurationCompletion | NoRetryCuration | RetryCuration,
        *,
        duration_ms: float,
    ) -> None:
        logger = self._logger.bind(duration_ms=duration_ms)
        match consume_result:
            case CurationCompletion(kind=kind, curation_id=None):
                logger.info(
                    "curation_message_processing_completed",
                    outcome=kind.value,
                    message_disposition="completed",
                )
            case CurationCompletion(kind=kind, curation_id=curation_id):
                logger.bind(curation_id=curation_id).info(
                    "curation_message_processing_completed",
                    outcome=kind.value,
                    message_disposition="completed",
                )
            case RetryCuration(error=error):
                logger.error(
                    "curation_message_processing_failed",
                    message_disposition="batch_item_failure",
                    exc_info=error,
                )
            case NoRetryCuration(cause=CurationReadyBuildRejected(reason=reason)):
                logger.warning(
                    "curation_message_processing_failed",
                    operation="build_ready",
                    rejection_code=reason.value,
                    message_disposition="completed",
                )
            case NoRetryCuration(cause=cause):
                logger.warning(
                    "curation_message_processing_failed",
                    code=cause.CODE,
                    failure_reason=cause.reason.value,
                    message_disposition="completed",
                )
