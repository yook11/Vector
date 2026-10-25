"""Lambdaが渡したSQSのレコード1件を、工程が受け付けるイベントとして読む。"""

from collections.abc import Callable
from dataclasses import dataclass

from app.aws.lambda_.sqs_input import SqsLambdaRecord


@dataclass(frozen=True, slots=True)
class EventReadFailed:
    """受信メッセージをイベントとして読めなかった原因。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class EventReader[EventT]:
    """Lambdaが渡したSQSのレコード1件を、Consumerが受け付けるイベントとして読む。"""

    parse_event: Callable[[object], EventT]

    def read(self, record: SqsLambdaRecord) -> EventT | EventReadFailed:
        """本文・JSON・イベント契約のどこで失敗しても、原因の例外を値で返す。"""
        try:
            return self.parse_event(record.read_message().parse_json())
        except Exception as exc:
            return EventReadFailed(exc)
