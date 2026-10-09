"""SQSのレコード1件をイベントとして読み、失敗は原因の例外を持つ値で返すことを検証する。"""

import pytest

from app.lambda_handlers.event_reader import EventReader, EventReadFailed
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError
from app.lambda_handlers.sqs.records import SqsRecordInput

pytestmark = pytest.mark.unit


def _record_input(record: dict[str, object]) -> SqsRecordInput:
    return SqsRecordInput.from_lambda_record(record, record_index=0)


def test_parsed_body_is_passed_to_parser_and_its_event_returned():
    """JSONとして読んだ本文をイベントの解析関数へ渡し、その結果を返す。"""
    event_reader = EventReader(lambda data: ("event", data))

    read_result = event_reader.read(
        _record_input({"messageId": "id", "body": '{"key": [1, true]}'})
    )

    assert read_result == ("event", {"key": [1, True]})


@pytest.mark.parametrize(
    ("record", "error_class"),
    [
        pytest.param({"messageId": "id"}, SqsInputError, id="missing-body"),
        pytest.param({"messageId": "id", "body": 1}, SqsInputError, id="non-str-body"),
        pytest.param(
            {"messageId": "id", "body": "{"},
            SqsMessageJsonInvalidError,
            id="invalid-json",
        ),
    ],
)
def test_body_and_json_failures_are_returned_as_read_failure(record, error_class):
    """本文とJSONの不正は解析関数を呼ばずに、その例外を持つ読み取り失敗として返す。"""

    def parse_event(data: object) -> object:
        raise AssertionError("parser must not be called")

    read_result = EventReader(parse_event).read(_record_input(record))

    assert (type(read_result), type(getattr(read_result, "error", None))) == (
        EventReadFailed,
        error_class,
    )


def test_parser_exception_is_returned_as_read_failure():
    """解析関数の例外は、その例外を持つ読み取り失敗として返す。"""
    original = RuntimeError("parser-failed")

    def parse_event(data: object) -> object:
        raise original

    read_result = EventReader(parse_event).read(
        _record_input({"messageId": "id", "body": "{}"})
    )

    assert read_result == EventReadFailed(original)
