from collections.abc import Sequence
from dataclasses import dataclass

from app.outbox.publishing.route import EventMessage


@dataclass(frozen=True, slots=True, init=False)
class MessageBatch:
    messages: tuple[EventMessage, ...]

    def __init__(self, messages: Sequence[EventMessage]) -> None:
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise TypeError("messages must be a sequence of EventMessage")
        messages = tuple(messages)
        if not messages:
            raise ValueError("batch must not be empty")
        if any(not isinstance(message, EventMessage) for message in messages):
            raise TypeError("invalid EventMessage")
        if len({message.event_id for message in messages}) != len(messages):
            raise ValueError("batch message IDs must be unique")
        object.__setattr__(self, "messages", messages)
