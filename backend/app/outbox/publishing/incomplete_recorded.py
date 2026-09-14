"""未完成記事記録イベントを検証し、既存形式の送信本文を作る。"""

import json

from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
    IncompleteArticleRecordedEvent,
)
from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
)
from app.outbox.publishing.publisher import EventEnvelope
from app.outbox.publishing.route import EventMessage


def build_incomplete_recorded_message(
    envelope: EventEnvelope,
) -> EventMessage:
    """未完成記事記録イベントを検証し、既存のJSON表現で本文を返す。"""
    try:
        event = IncompleteArticleRecordedEvent.from_input(
            {
                "event_id": envelope.event_id,
                "event_type": envelope.event_type,
                "schema_version": envelope.schema_version,
                "occurred_at": envelope.occurred_at,
                "payload": envelope.payload,
            }
        )
    except IncompleteArticleEventInvalidError as exc:
        invalid = exc.invalid
        reason = PublishEventInvalidReason(invalid.reason)
    else:
        return EventMessage(
            event_id=event.event_id,
            body=json.dumps(event.model_dump(mode="json"), allow_nan=False),
        )
    raise PublishEventInvalidError(reason=reason, issues=invalid.issues)
