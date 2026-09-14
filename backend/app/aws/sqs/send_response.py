"""SQSのSendMessage応答で確認する項目と本文の整合性を表す。"""

from hashlib import md5
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.aws.sqs.errors import SqsSendError, SqsSendFailure, SqsSendFailureKind


class SqsSendResponse(BaseModel):
    """SQSの送信応答の形式を検証し、送信した本文との照合を担う。"""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="ignore", hide_input_in_errors=True
    )

    message_id: str = Field(alias="MessageId", pattern=r"\S", repr=False)
    body_checksum: str = Field(
        alias="MD5OfMessageBody", pattern=r"^[0-9a-fA-F]{32}$", repr=False
    )

    @classmethod
    def from_response(cls, response: object) -> Self:
        try:
            return cls.model_validate(response)
        except ValidationError:
            raise SqsSendError(
                SqsSendFailure(SqsSendFailureKind.INVALID_RESPONSE)
            ) from None

    def verify_body(self, body: str) -> None:
        expected = md5(body.encode("utf-8"), usedforsecurity=False).hexdigest()
        if self.body_checksum.lower() != expected:
            raise SqsSendError(SqsSendFailure(SqsSendFailureKind.BODY_MISMATCH))
