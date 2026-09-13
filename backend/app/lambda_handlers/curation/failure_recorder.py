"""Curation Lambdaの入力・処理・資源管理の失敗を安全に記録する。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from structlog.stdlib import BoundLogger

from app.audit.error_fields import exception_fqn

if TYPE_CHECKING:
    from app.collection.events import AnalyzableArticleCreatedEvent
    from app.lambda_handlers.curation.event import CurationEventInvalidError
    from app.lambda_handlers.sqs.errors import SqsInputError


class CurationLambdaFailureRecorder:
    """診断の障害で元の例外や利用結果を変更しない。"""

    def __init__(self, logger: BoundLogger) -> None:
        self._logger = logger

    def record_initialization_failure(self, stage: str, error: Exception) -> None:
        self._record(
            "curation_initialization_failed",
            stage=stage,
            error_class=exception_fqn(error),
        )

    def record_cleanup_failure(self, resource: str, error: Exception) -> None:
        self._record(
            "curation_resources_cleanup_failed",
            resource=resource,
            error_class=exception_fqn(error),
        )

    def record_invalid_sqs_input(self, error: SqsInputError) -> None:
        self._record(
            "curation_sqs_input_invalid",
            reason=error.reason.value,
            field=error.field,
            record_index=error.record_index,
        )

    def record_invalid_body(self, error: SqsInputError, *, message_id: str) -> None:
        self._record(
            "curation_message_input_invalid",
            message_id=message_id,
            reason="invalid_body",
            issues=[{"field": "body", "code": error.reason.value}],
        )

    def record_invalid_event(
        self, error: CurationEventInvalidError, *, message_id: str
    ) -> None:
        self._record(
            "curation_message_input_invalid",
            message_id=message_id,
            reason=error.reason.value,
            issues=[
                {"field": issue.field.value, "code": issue.code.value}
                for issue in error.issues
            ],
        )

    def record_message_failure(
        self,
        error: Exception,
        *,
        message_id: str,
        article_event: AnalyzableArticleCreatedEvent | None = None,
    ) -> None:
        fields: dict[str, object] = {"message_id": message_id}
        if article_event is not None:
            fields.update(
                event_id=str(article_event.event_id),
                analyzable_article_id=article_event.payload.analyzable_article_id,
            )
        self._record(
            "curation_message_failed", **fields, error_class=exception_fqn(error)
        )

    def _record(self, event: str, **fields: object) -> None:
        try:
            self._logger.warning(event, **fields)
        except Exception:  # noqa: S110
            # 診断障害で利用結果や元の例外を置き換えない。
            pass
