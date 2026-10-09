"""SQSのレコード1件を、工程が受け付けるイベントとして読む。"""

from collections.abc import Callable
from dataclasses import dataclass

from app.lambda_handlers.sqs.records import SqsRecordInput


@dataclass(frozen=True, slots=True)
class EventReadFailed:
    """レコードをイベントとして読めなかった原因。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class EventReader[EventT]:
    """SQSのレコード1件を、Consumerが受け付けるイベントとして読む。"""

    parse_event: Callable[[object], EventT]

    def read(self, message_record: SqsRecordInput) -> EventT | EventReadFailed:
        """本文・JSON・イベント契約のどこで失敗しても、原因の例外を値で返す。"""
        try:
            return self.parse_event(message_record.to_record().parse_json())
        except Exception as exc:
            return EventReadFailed(exc)
