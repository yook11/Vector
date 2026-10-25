"""受信したSQSメッセージ1件をイベントとして読み、失敗は原因の例外を持つ値で返すことを検証する。"""

import pytest

from app.aws.lambda_.sqs_input import parse_sqs_lambda_input
from app.aws.sqs.message import ReceivedSqsMessage, SqsMessageInvalidReason
from app.lambda_handlers.event_reader import EventReader, EventReadFailed

pytestmark = pytest.mark.unit


def _received_message(record: dict[str, object]) -> ReceivedSqsMessage:
    return parse_sqs_lambda_input({"Records": [record]})[0]


def test_parsed_body_is_passed_to_parser_and_its_event_returned():
    """JSONとして読んだ本文をイベントの解析関数へ渡し、その結果を返す。"""
    event_reader = EventReader(lambda data: ("event", data))

    read_result = event_reader.read(
        _received_message(
            {
                "receiptHandle": "receipt-handle",
                "messageId": "id",
                "body": '{"key": [1, true]}',
            }
        )
    )

    assert read_result == ("event", {"key": [1, True]})


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        pytest.param(
            {"receiptHandle": "receipt-handle", "messageId": "id"},
            SqsMessageInvalidReason.MISSING_REQUIRED_FIELD,
            id="missing-body",
        ),
        pytest.param(
            {"receiptHandle": "receipt-handle", "messageId": "id", "body": 1},
            SqsMessageInvalidReason.INVALID_TYPE,
            id="non-str-body",
        ),
        pytest.param(
            {"receiptHandle": "receipt-handle", "messageId": "id", "body": "{"},
            SqsMessageInvalidReason.INVALID_JSON,
            id="invalid-json",
        ),
    ],
)
def test_body_and_json_failures_are_returned_as_read_failure(record, reason):
    """本文とJSONの不正は解析関数を呼ばずに、その例外を持つ読み取り失敗として返す。"""

    def parse_event(data: object) -> object:
        raise AssertionError("parser must not be called")

    read_result = EventReader(parse_event).read(_received_message(record))

    assert isinstance(read_result, EventReadFailed)
    assert getattr(read_result.error, "reason", None) is reason


def test_parser_exception_is_returned_as_read_failure():
    """解析関数の例外は、その例外を持つ読み取り失敗として返す。"""
    original = RuntimeError("parser-failed")

    def parse_event(data: object) -> object:
        raise original

    read_result = EventReader(parse_event).read(
        _received_message(
            {"receiptHandle": "receipt-handle", "messageId": "id", "body": "{}"}
        )
    )

    assert read_result == EventReadFailed(original)
