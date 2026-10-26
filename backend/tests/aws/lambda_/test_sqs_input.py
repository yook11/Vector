"""Lambdaへの入力をSQSメッセージへ変換するときの検証を、業務イベントに依存せずDB・外部通信なしで確認する。"""

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
    InvalidSqsMessage,
    SqsMessage,
    SqsMessageInvalidReason,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("body", ["", "not-json", '{"event_type":"other.event"}'])
def test_batch_preserves_body_without_interpreting_business_event(body):
    """イベントの意味や本文の妥当性を解釈せず、受信順と本文・受信ハンドルを保持する。"""
    messages = parse_sqs_lambda_input(
        {
            "Records": [
                {"messageId": " second ", "receiptHandle": "receipt-2", "body": body},
                {
                    "messageId": "first",
                    "receiptHandle": "receipt-1",
                    "body": "another-body",
                },
            ]
        }
    )
    assert messages == (
        SqsMessage(message_id=" second ", receipt_handle="receipt-2", body=body),
        SqsMessage(message_id="first", receipt_handle="receipt-1", body="another-body"),
    )


def test_missing_body_is_returned_as_invalid_message():
    """本文が欠落した1件は、全体を止めずにIDを持つ本文不正のメッセージとして返す。"""
    [message] = parse_sqs_lambda_input(
        {"Records": [{"messageId": "missing-body", "receiptHandle": "receipt"}]}
    )

    assert isinstance(message, InvalidSqsMessage)
    assert message.message_id == "missing-body"
    assert message.error.reason is SqsMessageInvalidReason.MISSING_REQUIRED_FIELD
    assert message.error.field == "body"


@pytest.mark.parametrize("body", [None, 3, True, {}, [], b"private-body"])
def test_non_string_body_does_not_prevent_following_message(body):
    """本文の型不正を個別の本文不正とし、後続メッセージの変換を妨げない。"""
    first, second = parse_sqs_lambda_input(
        {
            "Records": [
                {"messageId": "bad", "receiptHandle": "receipt-bad", "body": body},
                {
                    "messageId": "good",
                    "receiptHandle": "receipt-good",
                    "body": '{"usable": true}',
                },
            ]
        }
    )
    assert isinstance(first, InvalidSqsMessage)
    assert first.error.reason is SqsMessageInvalidReason.INVALID_TYPE
    assert first.error.field == "body"
    assert second == SqsMessage(
        message_id="good", receipt_handle="receipt-good", body='{"usable": true}'
    )


def test_duplicate_id_precedes_invalid_body_and_error_does_not_expose_input():
    """本文不正より先にID重複を拒否し、例外に入力値を保持しない。"""
    with pytest.raises(SqsLambdaInputInvalidError) as caught:
        parse_sqs_lambda_input(
            {
                "Records": [
                    {"messageId": "private-id", "receiptHandle": "private-receipt"},
                    {
                        "messageId": "private-id",
                        "receiptHandle": "private-receipt",
                        "body": "private-body",
                    },
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
    messages = [
        {"messageId": "valid", "receiptHandle": "receipt", "body": "body"},
        invalid_record,
    ]

    with pytest.raises(SqsLambdaInputInvalidError) as caught:
        parse_sqs_lambda_input({"Records": messages})

    assert caught.value.record_index == 1


@pytest.mark.parametrize(
    "record,reason",
    [
        pytest.param(
            {"messageId": "id", "body": "{}"},
            SqsLambdaInputInvalidReason.MISSING_REQUIRED_FIELD,
            id="missing",
        ),
        pytest.param(
            {"messageId": "id", "receiptHandle": None, "body": "{}"},
            SqsLambdaInputInvalidReason.INVALID_TYPE,
            id="null",
        ),
        pytest.param(
            {"messageId": "id", "receiptHandle": " \t", "body": "{}"},
            SqsLambdaInputInvalidReason.EMPTY_RECEIPT_HANDLE,
            id="blank",
        ),
    ],
)
def test_unusable_receipt_handle_rejects_whole_input(record, reason):
    """SQSが付ける受信ハンドルを使えなければ、IDと同じく入力全体を受け付けない。"""
    with pytest.raises(SqsLambdaInputInvalidError) as caught:
        parse_sqs_lambda_input({"Records": [record]})

    assert caught.value.reason is reason
    assert caught.value.field == "receiptHandle"
    assert caught.value.record_index == 0


def test_receipt_handle_preserves_original_text():
    """AWSから渡された操作情報の有効な文字列を加工しない。"""
    assert parse_sqs_lambda_input(
        {"Records": [{"messageId": "id", "receiptHandle": " handle ", "body": "{}"}]}
    ) == (SqsMessage(message_id="id", receipt_handle=" handle ", body="{}"),)
