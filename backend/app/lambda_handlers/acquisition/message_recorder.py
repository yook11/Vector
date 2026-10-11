"""取得メッセージの配送診断を、本文や例外の自由文を含めずに記録する。"""

from typing import assert_never

from structlog.stdlib import BoundLogger

from app.audit.error_fields import exception_fqn
from app.collection.article_acquisition.consumer_result import (
    AcquisitionSucceeded,
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.collection.article_acquisition.errors import AcquisitionSourceInvalidError
from app.collection.sources.acquisition_request import (
    AcquisitionRequestInvalidError,
    SourceAcquisitionRequest,
)
from app.lambda_handlers.event_reader import EventReadFailed
from app.lambda_handlers.sqs.received_message import ReceivedMessageInvalidError


class AcquisitionMessageRecorder:
    """入力・呼び出し・結果の診断を、配送判断から独立させる。"""

    def __init__(self, logger: BoundLogger) -> None:
        self._logger = logger

    def record_read_failed(
        self,
        read_failed: EventReadFailed,
        *,
        message_id: str,
        duration_ms: float,
    ) -> None:
        error = read_failed.error
        invalid_request = isinstance(
            error, AcquisitionRequestInvalidError | ReceivedMessageInvalidError
        )
        self._record(
            message_id=message_id,
            result="failed",
            code="invalid_request" if invalid_request else "processing_failed",
            operation="validate_event"
            if isinstance(error, AcquisitionRequestInvalidError)
            else "parse_message",
            error_class=exception_fqn(error),
            message_disposition="batch_item_failure",
            duration_ms=duration_ms,
        )

    def record_consume_failed(
        self,
        error: Exception,
        *,
        message_id: str,
        request: SourceAcquisitionRequest,
        duration_ms: float,
    ) -> None:
        self._record(
            message_id=message_id,
            request_id=request.request_id,
            source_id=request.source_id,
            result="failed",
            code="source_not_registered"
            if isinstance(error, AcquisitionSourceInvalidError)
            else "processing_failed",
            operation="consume",
            error_class=exception_fqn(error),
            message_disposition="batch_item_failure",
            duration_ms=duration_ms,
        )

    def record_consumed(
        self,
        result: AcquisitionSucceeded | RetryAcquisition | NoRetryAcquisition,
        *,
        message_id: str,
        request: SourceAcquisitionRequest,
        duration_ms: float,
    ) -> None:
        try:
            fields: dict[str, object] = {
                "message_id": message_id,
                "request_id": request.request_id,
                "source_id": request.source_id,
                "duration_ms": duration_ms,
                "message_disposition": "batch_item_failure"
                if isinstance(result, RetryAcquisition)
                else "completed",
            }
            match result:
                case AcquisitionSucceeded(created_count=created_count):
                    fields.update(result="acquired", created_count=created_count)
                case (
                    RetryAcquisition(error=error)
                    | NoRetryAcquisition(cause=Exception() as error)
                ):
                    fields.update(
                        result="failed",
                        code="processing_failed",
                        error_class=exception_fqn(error),
                    )
                case NoRetryAcquisition(cause=reason):
                    fields.update(result=reason, created_count=0)
                case _:
                    assert_never(result)
            self._record(**fields)
        except Exception:  # noqa: S110
            # 診断障害で確定した配送結果を変えない。
            pass

    def _record(self, **fields: object) -> None:
        try:
            self._logger.info("acquisition_message_processed", **fields)
        except Exception:  # noqa: S110
            # ログ出力の通常障害で配送判断を変えない。
            pass
