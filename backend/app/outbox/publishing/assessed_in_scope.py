"""対象内判定イベントを検証し、既存形式の送信本文を作る。"""

import json

from app.analysis.assessment.events import (
    ArticleAssessedInScopeEvent,
    AssessedEventValidationError,
)
from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
)
from app.outbox.publishing.publisher import EventEnvelope
from app.outbox.publishing.route import EventMessage


def build_assessed_in_scope_message(
    envelope: EventEnvelope,
) -> EventMessage:
    """対象内判定イベントを検証し、既存のJSON表現で本文を返す。"""
    try:
        event = ArticleAssessedInScopeEvent.from_input(
            {
                "event_id": envelope.event_id,
                "event_type": envelope.event_type,
                "schema_version": envelope.schema_version,
                "occurred_at": envelope.occurred_at,
                "payload": envelope.payload,
            }
        )
    except AssessedEventValidationError as exc:
        failure = exc.failure
        reason = PublishEventInvalidReason(failure.reason)
    else:
        return EventMessage(
            event_id=event.event_id,
            body=json.dumps(event.model_dump(mode="json"), allow_nan=False),
        )
    raise PublishEventInvalidError(reason=reason, issues=failure.issues)
