from __future__ import annotations

from app.outbox.relay.sender import MessageSendError
from app.shared.event_contract import EventInvalidError

# 型注釈とisinstanceの両方に使うため、TypeAliasやtype文ではなくUnionTypeの値として持つ。
PublishFailure = MessageSendError | EventInvalidError
