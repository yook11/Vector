"""検証済みの入力から、送信可能な本文と個別の準備失敗を確定する。"""

from dataclasses import InitVar, dataclass, field

from app.analysis.assessment.events import (
    ArticleAssessedInScopeEvent,
    AssessedEventValidationError,
)
from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
    PublishPhase,
)
from app.outbox.publishing.publisher import EventEnvelope, PublishFailed
from app.outbox.sqs.error_mapping import publish_error_from_exception
from app.outbox.sqs.event_batch import EventBatch
from app.outbox.sqs.message import MAX_MESSAGE_BYTES, SqsMessage


@dataclass(frozen=True, slots=True)
class SqsMessageBatch:
    """入力の件数・ID保証を引き継ぎ、本文合計サイズを検証する。"""

    events: InitVar[EventBatch]
    messages: tuple[SqsMessage, ...] = field(init=False)
    failures: tuple[PublishFailed, ...] = field(init=False)

    def __post_init__(self, events: EventBatch) -> None:
        if not isinstance(events, EventBatch):
            raise TypeError("events must be an EventBatch")
        messages: list[SqsMessage] = []
        failures: list[PublishFailed] = []
        for envelope in events.envelopes:
            try:
                event = _assessed_in_scope_event_from_envelope(envelope)
                message = SqsMessage.from_event(event)
            except Exception as exc:
                error = publish_error_from_exception(
                    exc, phase=PublishPhase.PREPARE_EVENT
                )
                failures.append(PublishFailed(envelope.event_id, error))
            else:
                messages.append(message)
        if (
            sum(len(message.body.encode("utf-8")) for message in messages)
            > MAX_MESSAGE_BYTES
        ):
            raise ValueError("batch message bodies exceed the size limit")
        object.__setattr__(self, "messages", tuple(messages))
        object.__setattr__(self, "failures", tuple(failures))


def _assessed_in_scope_event_from_envelope(
    envelope: EventEnvelope,
) -> ArticleAssessedInScopeEvent:
    """対象内判定イベントを検証し、型付きpayloadとともに返す。"""
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
        return event
    raise PublishEventInvalidError(reason=reason, issues=failure.issues)
