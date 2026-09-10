"""Embedding Lambdaの失敗診断を集約し、配送結果と元の例外を維持する。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from structlog.stdlib import BoundLogger

from app.audit.error_fields import exception_fqn

if TYPE_CHECKING:
    from app.analysis.assessment.events import ArticleAssessedInScopeEvent
    from app.lambda_handlers.embedding.event import EmbeddingEventInvalidError
    from app.lambda_handlers.sqs.errors import SqsInputError


class EmbeddingLambdaFailureRecorder:
    """安全な診断項目だけを記録し、再配信と例外伝播は呼び出し元に委ねる。"""

    def __init__(self, logger: BoundLogger) -> None:
        self._logger = logger

    def record_initialization_failure(self, stage: str, error: Exception) -> None:
        self._record(
            "embedding_initialization_failed",
            stage=stage,
            error_class=exception_fqn(error),
        )

    def record_cleanup_failure(self, resource: str, error: Exception) -> None:
        self._record(
            "embedding_resources_cleanup_failed",
            resource=resource,
            error_class=exception_fqn(error),
        )

    def record_invalid_sqs_input(self, error: SqsInputError) -> None:
        self._record(
            "embedding_sqs_input_invalid",
            reason=error.reason.value,
            field=error.field,
            record_index=error.record_index,
        )

    def record_invalid_body(self, error: SqsInputError, *, message_id: str) -> None:
        self._record(
            "embedding_message_input_invalid",
            message_id=message_id,
            reason="invalid_body",
            issues=[{"field": "body", "code": error.reason.value}],
        )

    def record_invalid_event(
        self, error: EmbeddingEventInvalidError, *, message_id: str
    ) -> None:
        self._record(
            "embedding_message_input_invalid",
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
        assessed_event: ArticleAssessedInScopeEvent | None = None,
    ) -> None:
        fields: dict[str, object] = {"message_id": message_id}
        if assessed_event is not None:
            fields.update(
                event_id=str(assessed_event.event_id),
                analyzed_article_id=assessed_event.payload.analyzed_article_id,
            )
        self._record(
            "embedding_message_failed", **fields, error_class=exception_fqn(error)
        )

    def _record(self, event: str, **fields: object) -> None:
        try:
            self._logger.warning(event, **fields)
        except Exception:  # noqa: S110
            # 診断出力の障害で配送結果や元の例外を変えない。
            pass
