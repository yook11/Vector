"""補完配送の診断を入力本文や例外の自由文を含めずに記録する。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from structlog.stdlib import BoundLogger

from app.audit.error_fields import exception_fqn

if TYPE_CHECKING:
    from app.collection.article_acquisition.events import (
        IncompleteArticleEventInvalidError,
        IncompleteArticleRecordedEvent,
    )
    from app.lambda_handlers.sqs.errors import SqsInputError


class CompletionLambdaFailureRecorder:
    """入力・配送処理の失敗を、診断障害から独立させる。"""

    def __init__(self, logger: BoundLogger) -> None:
        self._logger = logger

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

    def _record(self, event: str, **fields: object) -> None:
        try:
            self._logger.warning(event, **fields)
        except Exception:  # noqa: S110
            # 診断障害で元の配送結果を置き換えない。
            pass
