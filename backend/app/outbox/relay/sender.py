from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.http.failure import HttpTransportFailure
from app.outbox.event.message import EventMessage
from app.shared.errors import ApplicationError


@dataclass(frozen=True, slots=True)
class MessageAccepted:
    """送信先が本文を受け付けた結果で、受信後の処理完了は表さない。"""

    message_id: str


class MessageServiceErrorReason(StrEnum):
    """送信先が返したエラーコードを、何が起きたかの意味で分けた理由。"""

    THROTTLED = "throttled"
    SERVICE_UNAVAILABLE = "service_unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCESS_DENIED = "access_denied"
    DESTINATION_NOT_FOUND = "destination_not_found"
    REQUEST_REJECTED = "request_rejected"
    SECURITY_REJECTED = "security_rejected"
    REQUEST_EXPIRED = "request_expired"
    ENCRYPTION_ERROR = "encryption_error"
    UNKNOWN = "unknown"


class MessageSendConfigurationReason(StrEnum):
    """送信に必要な設定のうち、何がなかったか。"""

    MISSING_CREDENTIALS = "missing_credentials"
    INCOMPLETE_CREDENTIALS = "incomplete_credentials"
    MISSING_REGION = "missing_region"
    MISSING_QUEUE_URL = "missing_queue_url"


class MessageSendError(ApplicationError):
    """送れなかった失敗の基底で、再送するかは受け取った工程が事実から決める。"""


class MessageSendTransportError(MessageSendError):
    """通信が完了しなかった失敗で、送信先が受け付けたかは断定しない。"""

    def __init__(self, *, failure: HttpTransportFailure, exception_type: str) -> None:
        self.failure = failure
        self.exception_type = exception_type
        super().__init__(
            "メッセージの送信で通信が完了しませんでした",
            details={
                "stage": self.failure.stage.value,
                "reason": self.failure.reason.value,
                "proxy_status": self.failure.proxy_status,
                "exception_type": self.exception_type,
            },
        )


class MessageServiceError(MessageSendError):
    """送信先がエラーコードを返して送信を終えた。"""

    def __init__(
        self, *, reason: MessageServiceErrorReason, service_code: str | None
    ) -> None:
        self.reason = reason
        self.service_code = service_code
        super().__init__(
            "送信先がエラーを返し、メッセージを送信できませんでした",
            details={"reason": self.reason.value, "service_code": self.service_code},
        )


class MessageSendConfigurationError(MessageSendError):
    """資格情報・リージョン・送信先のキューなど、送信に必要な設定がない。"""

    def __init__(self, *, reason: MessageSendConfigurationReason) -> None:
        self.reason = reason
        super().__init__(
            "メッセージの送信に必要な設定がありません",
            details={"reason": self.reason.value},
        )


class MessageSender(Protocol):
    async def send_batch[T](
        self, sendable: Sequence[tuple[T, EventMessage]]
    ) -> list[tuple[T, MessageAccepted | MessageSendError]]:
        """組のメッセージを開いたときのキューへまとめて送り、組の対象と受付か失敗を渡した順で返す。"""
        ...


type MessageSenderFactory = Callable[[], AbstractAsyncContextManager[MessageSender]]
