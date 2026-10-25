"""業務イベントに依存しない受信構造と二段階の検証を、DB・外部通信なしで確認する。"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.aws.lambda_.sqs_input import (
    SqsLambdaInputInvalidError,
    SqsLambdaInputInvalidReason,
    parse_sqs_lambda_input,
)
from app.aws.sqs.message import (
    SqsMessageInvalidError,
    SqsMessageInvalidReason,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("body", ["", "not-json", '{"event_type":"other.event"}'])
def test_batch_preserves_body_without_interpreting_business_event(body):
    """イベントの意味や本文の妥当性を解釈せず、受信順と本文を保持する。"""
    messages = parse_sqs_lambda_input(
        {
            "Records": [
                {"messageId": " second ", "body": body},
                {"messageId": "first", "body": "another-body"},
            ]
        }
    )
    assert [message.message_id for message in messages] == [
        " second ",
        "first",
    ]
    assert [message.body for message in messages] == [
        body,
        "another-body",
    ]


def test_missing_body_is_rejected_when_reading_message():
    """本文欠落は全IDの確認後に当該メッセージを読むときだけ拒否する。"""
    messages = parse_sqs_lambda_input({"Records": [{"messageId": "missing-body"}]})

    with pytest.raises(SqsMessageInvalidError) as caught:
        messages[0].parse_json()

    assert caught.value.reason is SqsMessageInvalidReason.MISSING_REQUIRED_FIELD
    assert caught.value.field == "body"


@pytest.mark.parametrize("body", [None, 3, True, {}, [], b"private-body"])
def test_non_string_body_does_not_prevent_following_message(body):
    """本文の型不正を個別に拒否し、後続メッセージの読み取りを妨げない。"""
    messages = parse_sqs_lambda_input(
        {
            "Records": [
                {"messageId": "bad", "body": body},
                {"messageId": "good", "body": '{"usable": true}'},
            ]
        }
    )
    with pytest.raises(SqsMessageInvalidError) as caught:
        messages[0].parse_json()
    assert caught.value.reason is SqsMessageInvalidReason.INVALID_TYPE
    assert caught.value.field == "body"
    assert messages[1].parse_json() == {"usable": True}


def test_duplicate_id_precedes_invalid_body_and_error_does_not_expose_input():
    """本文不正より先にID重複を拒否し、例外に入力値を保持しない。"""
    with pytest.raises(SqsLambdaInputInvalidError) as caught:
        parse_sqs_lambda_input(
            {
                "Records": [
                    {"messageId": "private-id"},
                    {"messageId": "private-id", "body": "private-body"},
                ]
            }
        )
    assert caught.value.reason is SqsLambdaInputInvalidReason.DUPLICATE_MESSAGE_ID
    assert caught.value.field == "messageId"
    assert caught.value.record_index == 1
    assert "private-" not in str(caught.value)
    assert "private-" not in repr(vars(caught.value))


def test_sqs_input_loads_without_embedding_or_application_settings():
    """別プロセスで業務処理とアプリ設定を読み込まずに受信形式を使える。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.aws.lambda_.sqs_input "
            "import parse_sqs_lambda_input; "
            "import sys; "
            "assert parse_sqs_lambda_input({'Records': []})"
            " == (); "
            "assert not any(name.startswith('app.lambda_handlers.embedding') "
            "or name.startswith('app.analysis') for name in sys.modules); "
            "assert 'app.config' not in sys.modules",
        ],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "event",
    [None, [], {}, {"Records": None}, {"Records": {}}, {"Records": "private-input"}],
)
def test_invalid_delivery_structure_is_rejected(event):
    """配送構造が不正なら、入力値を保持しない検証例外として拒否する。"""
    with pytest.raises(SqsLambdaInputInvalidError) as caught:
        parse_sqs_lambda_input(event)

    assert "private-input" not in str(caught.value)


@pytest.mark.parametrize(
    "invalid_record",
    [
        None,
        {},
        {"messageId": None},
        {"messageId": 3},
        {"messageId": " "},
        {"messageId": ""},
    ],
)
def test_invalid_message_id_reports_record_position(invalid_record):
    """不正なレコードの位置を示し、バッチとして受け付けない。"""
    messages = [{"messageId": "valid", "body": "body"}, invalid_record]

    with pytest.raises(SqsLambdaInputInvalidError) as caught:
        parse_sqs_lambda_input({"Records": messages})

    assert caught.value.record_index == 1


@pytest.mark.parametrize(
    "handle,reason",
    [
        (None, SqsMessageInvalidReason.INVALID_TYPE),
        (123, SqsMessageInvalidReason.INVALID_TYPE),
        ("", SqsMessageInvalidReason.EMPTY_RECEIPT_HANDLE),
        (" \t", SqsMessageInvalidReason.EMPTY_RECEIPT_HANDLE),
    ],
)
def test_receipt_handle_validation_is_deferred(handle, reason):
    """受信情報の不正はバッチ生成時でなく操作情報の取得時に拒否する。"""
    messages = parse_sqs_lambda_input(
        {"Records": [{"messageId": "id", "body": "{}", "receiptHandle": handle}]}
    )
    with pytest.raises(SqsMessageInvalidError) as caught:
        messages[0].receipt_handle_text()
    assert caught.value.reason is reason
    assert caught.value.field == "receiptHandle"


def test_missing_receipt_handle_is_distinct_from_invalid_type():
    """receiptHandleの欠落は型不正と区別する。"""
    messages = parse_sqs_lambda_input({"Records": [{"messageId": "id", "body": "{}"}]})
    with pytest.raises(SqsMessageInvalidError) as caught:
        messages[0].receipt_handle_text()
    assert caught.value.reason is SqsMessageInvalidReason.MISSING_REQUIRED_FIELD


def test_receipt_handle_preserves_original_text():
    """AWSから渡された操作情報の有効な文字列を加工しない。"""
    messages = parse_sqs_lambda_input(
        {"Records": [{"messageId": "id", "body": "{}", "receiptHandle": " handle "}]}
    )
    assert messages[0].receipt_handle_text() == " handle "
