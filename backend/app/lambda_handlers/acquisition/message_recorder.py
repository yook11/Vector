"""取得のメッセージ処理の開始と終わり方を、外部取得のログ方針の形で記録する。"""

from typing import assert_never

from structlog.typing import FilteringBoundLogger

from app.collection.article_acquisition.consumer_result import (
    AcquisitionSucceeded,
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.collection.sources.acquisition_request import AcquisitionRequestInvalidError
from app.lambda_handlers.event_reader import EventReadFailed


class AcquisitionMessageRecorder:
    """メッセージの終わり方ごとに、終端ログのイベント名・レベル・項目を決める。"""

    def __init__(self, logger: FilteringBoundLogger) -> None:
        self._logger = logger

    def record_started(self) -> None:
        self._logger.info("acquisition_message_processing_started")

    def record_read_failed(
        self, read_failed: EventReadFailed, *, duration_ms: float
    ) -> None:
        """本文・JSONを読めなかったか、取得依頼の契約に合わなかったかを区別して記録する。"""
        operation = (
            "validate_event"
            if isinstance(read_failed.error, AcquisitionRequestInvalidError)
            else "parse_message"
        )
        self._logger.error(
            "acquisition_message_processing_failed",
            operation=operation,
            duration_ms=duration_ms,
            message_disposition="batch_item_failure",
            exc_info=read_failed.error,
        )

    def record_consume_failed(self, error: Exception, *, duration_ms: float) -> None:
        self._logger.error(
            "acquisition_message_processing_failed",
            operation="consume",
            duration_ms=duration_ms,
            message_disposition="batch_item_failure",
            exc_info=error,
        )

    def record_consumed(
        self,
        result: AcquisitionSucceeded | RetryAcquisition | NoRetryAcquisition,
        *,
        duration_ms: float,
    ) -> None:
        """Consumerの結果から終わり方を決め、配送判断は変更しない。"""
        logger = self._logger.bind(duration_ms=duration_ms)
        match result:
            case AcquisitionSucceeded(created_count=created_count):
                logger.info(
                    "acquisition_message_processing_completed",
                    outcome="acquired",
                    created_count=created_count,
                    message_disposition="completed",
                )
            case RetryAcquisition(
                error=error, code=code, requires_investigation=investigate
            ):
                # 人の調査が要る失敗だけをerrorにし、想定内の失敗はwarningにする。
                (logger.error if investigate else logger.warning)(
                    "acquisition_message_processing_failed",
                    code=code,
                    requires_investigation=investigate,
                    message_disposition="batch_item_failure",
                    exc_info=error,
                )
            case NoRetryAcquisition(
                cause=Exception() as error,
                code=code,
                requires_investigation=investigate,
            ):
                (logger.error if investigate else logger.warning)(
                    "acquisition_message_processing_failed",
                    code=code,
                    requires_investigation=investigate,
                    message_disposition="completed",
                    exc_info=error,
                )
            case NoRetryAcquisition(cause=reason):
                logger.info(
                    "acquisition_message_processing_completed",
                    outcome=reason,
                    message_disposition="completed",
                )
            case _:
                assert_never(result)
