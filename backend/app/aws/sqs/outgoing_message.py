"""SQSへ送る本文と、受付応答の照合に使うMD5を一緒に保持する。"""

import re
from dataclasses import dataclass, field
from hashlib import md5

from app.aws.sqs.errors import MessageSendError, SqsSendFailureKind


@dataclass(frozen=True, slots=True)
class OutgoingSqsMessage:
    """送る本文から期待するMD5を確定し、SendMessageの応答がその本文を受け付けたかを照合する。"""

    body: str = field(repr=False)
    body_md5: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "body_md5",
            md5(self.body.encode("utf-8"), usedforsecurity=False).hexdigest(),
        )

    def verify(self, response: object) -> None:
        fields = response if isinstance(response, dict) else {}
        message_id = fields.get("MessageId")
        checksum = fields.get("MD5OfMessageBody")
        if (
            not isinstance(message_id, str)
            or not message_id.strip()
            or not isinstance(checksum, str)
            or re.fullmatch(r"[0-9a-fA-F]{32}", checksum) is None
        ):
            raise MessageSendError(SqsSendFailureKind.INVALID_RESPONSE)
        if checksum.lower() != self.body_md5:
            raise MessageSendError(SqsSendFailureKind.BODY_MISMATCH)
