"""SQSへ送る本文と、応答の照合に使う情報を一緒に保持する。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import md5
from uuid import UUID

from app.analysis.assessment.events import ArticleAssessedInScopeEvent
from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
)

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
    def from_event(cls, event: ArticleAssessedInScopeEvent) -> SqsMessage:
        """検証済みイベントを送信本文にし、送れなければ失敗を返す。"""
        body = json.dumps(event.model_dump(mode="json"), allow_nan=False)
        if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise PublishEventInvalidError(
                reason=PublishEventInvalidReason.MESSAGE_TOO_LARGE
            )
        return cls(event_id=event.event_id, body=body)
