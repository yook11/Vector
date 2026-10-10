"""SQSの受信メッセージ1件を、工程が受け付けるイベントとして読む。"""

from collections.abc import Callable
from dataclasses import dataclass

from app.lambda_handlers.sqs.received_message import ReceivedMessage


@dataclass(frozen=True, slots=True)
class EventReadFailed:
    """受信メッセージをイベントとして読めなかった原因。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class EventReader[EventT]:
    """SQSの受信メッセージ1件を、Consumerが受け付けるイベントとして読む。"""

    parse_event: Callable[[object], EventT]

    def read(self, message: ReceivedMessage) -> EventT | EventReadFailed:
        """本文・JSON・イベント契約のどこで失敗しても、原因の例外を値で返す。"""
        try:
            return self.parse_event(message.parse_json())
        except Exception as exc:
            return EventReadFailed(exc)
