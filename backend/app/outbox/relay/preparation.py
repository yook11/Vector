"""イベントを宛先と契約で確かめ、送信できる本文にする。"""

from collections.abc import Callable

from app.outbox.event.envelope import EventEnvelope
from app.outbox.event.message import EventMessage
from app.outbox.relay.route import EventDeliveryRoute


def build_sendable_message(
    envelope: EventEnvelope,
    *,
    route: EventDeliveryRoute,
    build_message: Callable[[EventEnvelope], EventMessage],
) -> EventMessage:
    """宛先と契約を満たすイベントを本文にし、満たさなければ宛先違いか契約違反の例外を送出する。"""
    route.ensure_matches(envelope)
    message = build_message(envelope)
    if not isinstance(message, EventMessage) or message.event_id != envelope.event_id:
        raise TypeError("message must preserve input event ID")
    return message
