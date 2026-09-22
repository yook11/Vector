"""SQS入力エラーを共通変換へ渡したときの診断結果を確認する。"""

import pytest

from app.lambda_handlers.sqs.errors import SqsInputError, SqsInputReason
from app.log_policy.exceptions.conversion import convert_exception

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("record_index", [0, 2])
def test_sqs_input_error_preserves_reason_field_and_record_position(
    record_index: int,
) -> None:
    """理由と不正項目に加え、先頭の0を含むレコード位置を診断に残す。"""
    error = SqsInputError(
        reason=SqsInputReason.MISSING_REQUIRED_FIELD,
        field="messageId",
        record_index=record_index,
    )

    converted_error = convert_exception(error)

    assert (
        converted_error.message == "SQS input validation failed: missing_required_field"
    )
    assert converted_error.error_details == {
        "reason": "missing_required_field",
        "field": "messageId",
        "record_index": record_index,
    }


def test_sqs_input_error_omits_unspecified_record_position() -> None:
    """レコード位置が未指定なら、理由と不正項目だけを診断に残す。"""
    error = SqsInputError(
        reason=SqsInputReason.INVALID_TYPE,
        field="Records",
    )

    converted_error = convert_exception(error)

    assert converted_error.message == "SQS input validation failed: invalid_type"
    assert converted_error.error_details == {
        "reason": "invalid_type",
        "field": "Records",
    }
