"""受信したSQSメッセージ1件を、工程が受け付けるイベントとして読む。"""

from collections.abc import Callable
from dataclasses import dataclass

from app.aws.sqs.message import InvalidSqsMessage, ReceivedSqsMessage


@dataclass(frozen=True, slots=True)
class EventReadFailed:
    """受信メッセージをイベントとして読めなかった原因。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class EventReader[EventT]:
    """受信したSQSメッセージ1件を、Consumerが受け付けるイベントとして読む。"""

    parse_event: Callable[[object], EventT]

    def read(self, message: ReceivedSqsMessage) -> EventT | EventReadFailed:
        """本文・JSON・イベント契約のどこで失敗しても、原因の例外を値で返す。"""
        if isinstance(message, InvalidSqsMessage):
            return EventReadFailed(message.error)
        try:
            return self.parse_event(message.parse_json())
        except Exception as exc:
            return EventReadFailed(exc)
