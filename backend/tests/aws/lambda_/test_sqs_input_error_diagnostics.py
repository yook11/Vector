"""受信構造の検証例外を共通変換へ渡した診断結果を、DB・外部通信なしで確認する。"""

import pytest

from app.aws.lambda_.sqs_input import (
    SqsLambdaInputInvalidError,
    SqsLambdaInputInvalidReason,
)
from app.log_policy.exceptions.conversion import convert_exception

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("record_index", [0, 2])
def test_batch_error_preserves_reason_field_and_record_position(
    record_index: int,
) -> None:
    """理由と不正項目に加え、先頭の0を含むレコード位置を診断に残す。"""
    error = SqsLambdaInputInvalidError(
        reason=SqsLambdaInputInvalidReason.MISSING_REQUIRED_FIELD,
        field="messageId",
        record_index=record_index,
    )

    converted_error = convert_exception(error)

    assert (
        converted_error.message
        == "Received message batch validation failed: missing_required_field"
    )
    assert converted_error.error_details == {
        "reason": "missing_required_field",
        "field": "messageId",
        "record_index": record_index,
    }


def test_batch_error_omits_unspecified_record_position() -> None:
    """レコード位置が未指定なら、理由と不正項目だけを診断に残す。"""
    error = SqsLambdaInputInvalidError(
        reason=SqsLambdaInputInvalidReason.INVALID_TYPE,
        field="Records",
    )

    converted_error = convert_exception(error)

    assert (
        converted_error.message
        == "Received message batch validation failed: invalid_type"
    )
    assert converted_error.error_details == {
        "reason": "invalid_type",
        "field": "Records",
    }
