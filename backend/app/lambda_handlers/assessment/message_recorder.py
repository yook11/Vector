"""Assessmentのメッセージ処理の開始と終わり方を、AI分析のログ方針の形で記録する。"""

from structlog.typing import FilteringBoundLogger

from app.ai_providers.errors import AIProviderError
from app.analysis.assessment.consumer_failure_classification import (
    NoRetryAssessment,
    RetryAssessment,
)
from app.analysis.assessment.domain.ready import AssessmentReadyBuildRejected
from app.analysis.assessment.service import AssessmentCompletion
from app.analysis.curation.events import CuratedEventInvalidError
from app.lambda_handlers.event_reader import EventReadFailed
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError


class AssessmentMessageRecorder:
    """メッセージの終わり方ごとに、終端ログのイベント名・レベル・項目を決める。"""

    def __init__(self, logger: FilteringBoundLogger) -> None:
        self._logger = logger

    def record_started(self) -> None:
        self._logger.info("assessment_message_processing_started")

    def record_read_failed(
        self, read_failed: EventReadFailed, *, duration_ms: float
    ) -> None:
        logger = self._logger.bind(
            duration_ms=duration_ms, message_disposition="batch_item_failure"
        )
        match read_failed.error:
            case SqsInputError() | SqsMessageJsonInvalidError() as error:
                logger.warning(
                    "assessment_message_processing_failed",
                    operation="parse_message",
                    exc_info=error,
                )
            case CuratedEventInvalidError() as error:
                logger.warning(
                    "assessment_message_processing_failed",
                    operation="validate_event",
                    exc_info=error,
                )
            case error:
                logger.error(
                    "assessment_message_processing_failed",
                    operation="parse_message",
                    exc_info=error,
                )

    def record_consumed(
        self,
        consume_result: AssessmentCompletion | NoRetryAssessment | RetryAssessment,
        *,
        duration_ms: float,
    ) -> None:
        logger = self._logger.bind(duration_ms=duration_ms)
        match consume_result:
            case AssessmentCompletion(kind=kind, analyzed_article_id=None):
                logger.info(
                    "assessment_message_processing_completed",
                    outcome=kind.value,
                    message_disposition="completed",
                )
            case AssessmentCompletion(
                kind=kind, analyzed_article_id=analyzed_article_id
            ):
                logger.bind(analyzed_article_id=analyzed_article_id).info(
                    "assessment_message_processing_completed",
                    outcome=kind.value,
                    message_disposition="completed",
                )
            case RetryAssessment(error=error):
                logger.error(
                    "assessment_message_processing_failed",
                    message_disposition="batch_item_failure",
                    exc_info=error,
                )
            case NoRetryAssessment(cause=AssessmentReadyBuildRejected(reason=reason)):
                logger.warning(
                    "assessment_message_processing_failed",
                    operation="build_ready",
                    rejection_code=reason.value,
                    message_disposition="completed",
                )
            case NoRetryAssessment(cause=AIProviderError() as cause):
                logger.warning(
                    "assessment_message_processing_failed",
                    code=cause.CODE,
                    failure_reason=cause.reason.value,
                    message_disposition="completed",
                )
            case NoRetryAssessment(cause=cause):
                logger.warning(
                    "assessment_message_processing_failed",
                    code=cause.code,
                    message_disposition="completed",
                )
