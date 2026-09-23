"""業務イベントに依存しない受信構造と二段階の検証を確認する。"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.lambda_handlers.sqs.errors import SqsInputError, SqsInputReason
from app.lambda_handlers.sqs.records import SqsRecordBatch

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("body", ["", "not-json", '{"event_type":"other.event"}'])
def test_batch_preserves_body_without_interpreting_business_event(body):
    record_batch = SqsRecordBatch.from_lambda_event(
        {
            "Records": [
                {"messageId": " second ", "body": body},
                {"messageId": "first", "body": "another-body"},
            ]
        }
    )
    assert [record.message_id for record in record_batch.records] == [
        " second ",
        "first",
    ]
    assert [record.to_record().body for record in record_batch.records] == [
        body,
        "another-body",
    ]


def test_missing_body_is_rejected_when_creating_individual_record():
    """本文欠落は全IDの確認後に当該レコードだけで拒否する。"""
    record_batch = SqsRecordBatch.from_lambda_event(
        {"Records": [{"messageId": "missing-body"}]}
    )

    with pytest.raises(SqsInputError) as caught:
        record_batch.records[0].to_record()

    assert caught.value.reason is SqsInputReason.MISSING_REQUIRED_FIELD
    assert caught.value.field == "body"


@pytest.mark.parametrize("body", [None, 3, True, {}, [], b"private-body"])
def test_non_string_body_does_not_prevent_following_record(body):
    """本文の型不正を個別に拒否し、後続レコードの構築を妨げない。"""
    record_batch = SqsRecordBatch.from_lambda_event(
        {
            "Records": [
                {"messageId": "bad", "body": body},
                {"messageId": "good", "body": "usable-body"},
            ]
        }
    )
    with pytest.raises(SqsInputError) as caught:
        record_batch.records[0].to_record()
    assert caught.value.reason is SqsInputReason.INVALID_TYPE
    assert caught.value.field == "body"
    assert record_batch.records[1].to_record().body == "usable-body"


def test_duplicate_id_precedes_invalid_body_and_error_does_not_expose_input():
    with pytest.raises(SqsInputError) as caught:
        SqsRecordBatch.from_lambda_event(
            {
                "Records": [
                    {"messageId": "private-id"},
                    {"messageId": "private-id", "body": "private-body"},
                ]
            }
        )
    assert caught.value.CODE == "sqs_input_invalid"
    assert caught.value.reason is SqsInputReason.DUPLICATE_MESSAGE_ID
    assert caught.value.field == "messageId"
    assert caught.value.record_index == 1
    assert "private-" not in str(caught.value)
    assert "private-" not in repr(vars(caught.value))


def test_shared_records_load_without_embedding_or_application_settings():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.lambda_handlers.sqs.records import SqsRecordBatch; "
            "import sys; "
            "assert SqsRecordBatch.from_lambda_event({'Records': []}).records == (); "
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
    with pytest.raises(SqsInputError) as caught:
        SqsRecordBatch.from_lambda_event(event)

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

    with pytest.raises(SqsInputError) as caught:
        SqsRecordBatch.from_lambda_event({"Records": messages})

    assert caught.value.record_index == 1


@pytest.mark.parametrize(
    "handle,reason",
    [
        (None, SqsInputReason.INVALID_TYPE),
        (123, SqsInputReason.INVALID_TYPE),
        ("", SqsInputReason.EMPTY_RECEIPT_HANDLE),
        (" \t", SqsInputReason.EMPTY_RECEIPT_HANDLE),
    ],
)
def test_receipt_handle_validation_is_deferred(handle, reason):
    """受信情報の不正はバッチ生成時でなく操作情報の取得時に拒否する。"""
    batch = SqsRecordBatch.from_lambda_event(
        {"Records": [{"messageId": "id", "body": "{}", "receiptHandle": handle}]}
    )
    with pytest.raises(SqsInputError) as caught:
        batch.records[0].to_record().receipt_handle_text()
    assert caught.value.reason is reason
    assert caught.value.field == "receiptHandle"


def test_missing_receipt_handle_is_distinct_from_invalid_type():
    """receiptHandleの欠落は型不正と区別する。"""
    batch = SqsRecordBatch.from_lambda_event(
        {"Records": [{"messageId": "id", "body": "{}"}]}
    )
    with pytest.raises(SqsInputError) as caught:
        batch.records[0].to_record().receipt_handle_text()
    assert caught.value.reason is SqsInputReason.MISSING_REQUIRED_FIELD


def test_receipt_handle_preserves_original_text():
    """AWSから渡された操作情報の有効な文字列を加工しない。"""
    batch = SqsRecordBatch.from_lambda_event(
        {"Records": [{"messageId": "id", "body": "{}", "receiptHandle": " handle "}]}
    )
    assert batch.records[0].to_record().receipt_handle_text() == " handle "
