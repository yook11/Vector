"""補完配送の診断を入力本文や例外の自由文を含めずに記録する。"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from structlog.stdlib import BoundLogger

from app.audit.error_fields import exception_fqn
from app.collection.article_acquisition.events import IncompleteArticleEventInvalidError
from app.collection.article_completion.consumer import (
    CompletionFailed,
    CompletionNotRequired,
    CompletionSucceeded,
)
from app.collection.article_completion.consumer_failure_classification import (
    CloseArticleCompletion,
    RetryArticleCompletion,
)
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError

if TYPE_CHECKING:
    from app.collection.article_acquisition.events import (
        IncompleteArticleRecordedEvent,
    )
    from app.lambda_handlers.completion.redelivery_wait import RedeliveryWait


class CompletionLambdaFailureRecorder:
    """入力・配送処理の失敗を、診断障害から独立させる。"""

    def __init__(self, logger: BoundLogger) -> None:
        self._logger = logger

    def record_processing_error(
        self,
        error: Exception,
        *,
        message_id: str,
        article_event: IncompleteArticleRecordedEvent | None,
    ) -> None:
        """本文解析前の入力不正と、検証後の配送障害を区別して記録する。"""
        if article_event is not None:
            self.record_message_failure(
                error, message_id=message_id, article_event=article_event
            )
            return
        match error:
            case SqsInputError():
                self.record_invalid_body(error, message_id=message_id)
            case SqsMessageJsonInvalidError():
                self.record_invalid_json(message_id=message_id)
            case IncompleteArticleEventInvalidError():
                self.record_invalid_event(error, message_id=message_id)
            case _:
                self.record_message_failure(error, message_id=message_id)

    def record_completion(
        self,
        completion: CompletionSucceeded | CompletionNotRequired | CompletionFailed,
        *,
        message_id: str,
        article_event: IncompleteArticleRecordedEvent,
    ) -> None:
        """Consumerの結果から診断項目を選び、配送判断は変更しない。"""
        try:
            fields: dict[str, object] = {
                "message_id": message_id,
                "event_id": str(article_event.event_id),
                "incomplete_article_id": article_event.payload.incomplete_article_id,
            }
            match completion:
                case CompletionSucceeded(analyzable_article_id=article_id):
                    fields.update(result="succeeded", analyzable_article_id=article_id)
                case CompletionNotRequired(reason=reason):
                    fields.update(result="not_required", reason=reason)
                case CompletionFailed(decision=decision):
                    fields.update(
                        code=decision.code,
                        requires_investigation=decision.requires_investigation,
                    )
                    match decision:
                        case RetryArticleCompletion(retry_at=retry_at):
                            fields.update(
                                result="retry",
                                retry_at=retry_at.value.isoformat()
                                if retry_at
                                else None,
                            )
                        case CloseArticleCompletion():
                            fields["result"] = "closed"
                        case _:
                            assert_never(decision)
                case _:
                    assert_never(completion)
            self._logger.info("completion_message_processed", **fields)
        except Exception:  # noqa: S110
            # 診断障害で元の配送結果を置き換えない。
            pass

    def record_invalid_sqs_input(self, error: SqsInputError) -> None:
        self._record(
            "completion_sqs_input_invalid",
            reason=error.reason.value,
            field=error.field,
            record_index=error.record_index,
        )

    def record_invalid_body(self, error: SqsInputError, *, message_id: str) -> None:
        self._record(
            "completion_message_input_invalid",
            message_id=message_id,
            reason="invalid_body",
            issues=[{"field": "body", "code": error.reason.value}],
        )

    def record_invalid_json(self, *, message_id: str) -> None:
        self._record(
            "completion_message_input_invalid",
            message_id=message_id,
            reason="invalid_json",
            issues=[],
        )

    def record_invalid_event(
        self, error: IncompleteArticleEventInvalidError, *, message_id: str
    ) -> None:
        self._record(
            "completion_message_input_invalid",
            message_id=message_id,
            reason=error.invalid.reason.value,
            issues=[
                {"field": issue.field.value, "code": issue.code.value}
                for issue in error.invalid.issues
            ],
        )

    def record_message_failure(
        self,
        error: Exception,
        *,
        message_id: str,
        article_event: IncompleteArticleRecordedEvent | None = None,
    ) -> None:
        fields: dict[str, object] = {"message_id": message_id}
        if article_event is not None:
            fields.update(
                event_id=str(article_event.event_id),
                incomplete_article_id=article_event.payload.incomplete_article_id,
            )
        self._record(
            "completion_message_failed", **fields, error_class=exception_fqn(error)
        )

    def record_unstarted(self, *, message_id: str) -> None:
        self._record(
            "completion_message_unstarted",
            message_id=message_id,
            reason="insufficient_time",
        )

    def record_redelivery_wait(
        self,
        wait: RedeliveryWait,
        *,
        result: str,
        requested_seconds: int | None = None,
        applied_seconds: int | None = None,
        capped: bool | None = None,
        reason: str | None = None,
        error: Exception | None = None,
    ) -> None:
        fields: dict[str, object] = {
            "message_id": wait.message_id,
            "retry_at": wait.retry_at.value.isoformat(),
            "result": result,
            "requested_seconds": requested_seconds,
            "applied_seconds": applied_seconds,
            "capped": capped,
        }
        if reason is not None:
            fields["reason"] = reason
        if error is not None:
            fields["error_class"] = exception_fqn(error)
        self._record("completion_redelivery_wait", **fields)

    def _record(self, event: str, **fields: object) -> None:
        try:
            self._logger.warning(event, **fields)
        except Exception:  # noqa: S110
            # 診断障害で元の配送結果を置き換えない。
            pass
