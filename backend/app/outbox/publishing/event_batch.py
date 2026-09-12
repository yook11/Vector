"""SQSへ一度に送信を依頼できるイベント一覧を定義する。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.outbox.publishing.publisher import EventEnvelope

MAX_BATCH_MESSAGES = 10


@dataclass(frozen=True, slots=True, init=False)
class EventBatch:
    """件数・型・IDの一意性を保証した入力のスナップショット。"""

    envelopes: tuple[EventEnvelope, ...]

    def __init__(self, envelopes: Sequence[EventEnvelope]) -> None:
        if not isinstance(envelopes, Sequence) or isinstance(envelopes, (str, bytes)):
            raise TypeError("envelopes must be a sequence of EventEnvelope")
        envelopes = tuple(envelopes)
        if not 1 <= len(envelopes) <= MAX_BATCH_MESSAGES:
            raise ValueError("batch must contain between one and ten events")
        for envelope in envelopes:
            if (
                not isinstance(envelope, EventEnvelope)
                or not isinstance(envelope.event_id, UUID)
                or not isinstance(envelope.event_type, str)
                or type(envelope.schema_version) is not int
                or not isinstance(envelope.occurred_at, datetime)
                or not isinstance(envelope.payload, dict)
            ):
                raise TypeError("invalid EventEnvelope field type")
        if len({envelope.event_id for envelope in envelopes}) != len(envelopes):
            raise ValueError("batch event IDs must be unique")
        object.__setattr__(self, "envelopes", envelopes)
