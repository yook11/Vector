"""イベントを検証し、送信できるメッセージと変換失敗に分ける。"""

from collections.abc import Callable, Sequence

from app.analysis.assessment.events import AssessedEventInvalidError
from app.analysis.curation.events import CuratedEventInvalidError
from app.collection.article_acquisition.events import IncompleteArticleEventInvalidError
from app.collection.events import AnalyzableEventInvalidError
from app.outbox.publishing.contracts import (
    EventEnvelope,
    PublishFailed,
)
from app.outbox.publishing.event_batch import EventBatch
from app.outbox.publishing.route import (
    EventDeliveryRoute,
    EventMessage,
    EventRouteMismatchError,
)


def prepare_messages(
    envelopes: Sequence[EventEnvelope],
    *,
    route: EventDeliveryRoute,
    build_message: Callable[[EventEnvelope], EventMessage],
) -> tuple[tuple[EventMessage, ...], tuple[PublishFailed, ...]]:
    if not callable(build_message):
        raise TypeError("message preparation requires a message builder")
    events = EventBatch(envelopes)
    messages: list[EventMessage] = []
    failures: list[PublishFailed] = []
    for envelope in events.envelopes:
        try:
            route.ensure_matches(envelope)
            message = build_message(envelope)
            if (
                not isinstance(message, EventMessage)
                or message.event_id != envelope.event_id
            ):
                raise TypeError("message must preserve input event ID")
        except (
            AnalyzableEventInvalidError,
            IncompleteArticleEventInvalidError,
            AssessedEventInvalidError,
            CuratedEventInvalidError,
            EventRouteMismatchError,
        ) as exc:
            failures.append(PublishFailed(envelope.event_id, exc))
        else:
            messages.append(message)
    return tuple(messages), tuple(failures)
