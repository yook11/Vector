"""SQSへ送る本文と、応答の照合に使う情報を一緒に保持する。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC
from hashlib import md5
from uuid import UUID

from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
)
from app.outbox.publishing.publisher import EventEnvelope

MAX_MESSAGE_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class SqsMessage:
    """送信本文から照合用MD5を確定し、本文との対応を保つ。"""

    event_id: UUID
    body: str = field(repr=False)
    body_md5: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "body_md5",
            md5(self.body.encode("utf-8"), usedforsecurity=False).hexdigest(),
        )

    @classmethod
    def from_envelope(cls, envelope: EventEnvelope) -> SqsMessage:
        """イベントを送信本文にし、送れなければ失敗を返す。"""
        if envelope.occurred_at.utcoffset() is None:
            raise PublishEventInvalidError(
                reason=PublishEventInvalidReason.INVALID_OCCURRED_AT
            )
        occurred_at = envelope.occurred_at.astimezone(UTC).isoformat()
        body = {
            "event_id": str(envelope.event_id),
            "event_type": envelope.event_type,
            "schema_version": envelope.schema_version,
            "occurred_at": occurred_at.replace("+00:00", "Z"),
            "payload": envelope.payload,
        }
        try:
            body = json.dumps(body, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise PublishEventInvalidError(
                reason=PublishEventInvalidReason.SERIALIZATION_FAILED
            ) from exc
        if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise PublishEventInvalidError(
                reason=PublishEventInvalidReason.MESSAGE_TOO_LARGE
            )
        return cls(event_id=envelope.event_id, body=body)
