"""本文のJSON解析を、イベント契約に依存せず検証する。"""

import traceback

import pytest

from app.lambda_handlers.sqs.errors import SqsMessageJsonInvalidError
from app.lambda_handlers.sqs.records import SqsRecord
from app.shared.errors import ApplicationError

pytestmark = pytest.mark.unit


def test_json_object_is_decoded_without_validating_event_fields():
    """イベントとして未知の項目もJSONとしてそのまま復元する。"""
    record = SqsRecord(message_id="id", body='{"unknown": {"values": [1, true]}}')

    assert record.parse_json() == {"unknown": {"values": [1, True]}}


@pytest.mark.parametrize(
    ("body", "expected"),
    [("null", None), ("[]", []), ("1", 1), ("true", True), ('"text"', "text")],
)
def test_valid_json_root_is_not_restricted_to_event_objects(body, expected):
    """JSONルートの業務上の制約は後続のイベント型に委ねる。"""
    record = SqsRecord(message_id="id", body=body)

    assert record.parse_json() == expected


@pytest.mark.parametrize("body", ["", "{", '{"private-input":', "private-invalid-json"])
def test_invalid_syntax_does_not_retain_body_or_original_exception(body):
    """構文不正の本文と元の解析例外を診断用例外へ保持しない。"""
    record = SqsRecord(message_id="id", body=body)

    with pytest.raises(SqsMessageJsonInvalidError) as caught:
        record.parse_json()

    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert "private-" not in repr(vars(caught.value))
    assert "private-" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "body",
    [
        '{"private-key":1,"private-key":2}',
        '{"payload":{"private-key":1,"private-key":2}}',
    ],
)
def test_duplicate_keys_are_rejected_at_any_depth(body):
    """同名キーを上書きせず、階層を問わずJSON不正として拒否する。"""
    record = SqsRecord(message_id="id", body=body)

    with pytest.raises(SqsMessageJsonInvalidError) as caught:
        record.parse_json()

    assert caught.value.__context__ is None
    assert "private-key" not in str(caught.value)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_root_numbers_are_rejected(constant):
    """JSONルートの非標準数値定数を受け入れない。"""
    record = SqsRecord(message_id="id", body=constant)

    with pytest.raises(SqsMessageJsonInvalidError):
        record.parse_json()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nested_nonstandard_numbers_are_rejected(constant):
    """入れ子の値でも非標準数値定数を受け入れない。"""
    record = SqsRecord(message_id="id", body='{"payload":{"value":' + constant + "}}")

    with pytest.raises(SqsMessageJsonInvalidError):
        record.parse_json()


def test_deep_json_is_reported_without_original_exception():
    """深いネストによる解析失敗を入力を持たないJSON不正へ変換する。"""
    record = SqsRecord(message_id="id", body="[" * 10_000 + "0" + "]" * 10_000)

    with pytest.raises(SqsMessageJsonInvalidError) as caught:
        record.parse_json()

    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_json_error_preserves_application_error_diagnostics():
    """共通ログ基盤には固定メッセージとJSON不正の理由だけを渡す。"""
    error = SqsMessageJsonInvalidError()

    assert isinstance(error, ApplicationError)
    assert str(error) == "SQS message JSON parsing failed"
    assert error.details == {"reason": "invalid_json"}
