"""取得依頼の受信契約に固有の不正入力を検証する。"""

import json

import pytest

from app.collection.sources.acquisition_request import (
    AcquisitionRequestInvalidError,
    acquisition_request_from_message,
)
from app.lambda_handlers.sqs.records import SqsRecord


def message(**updates):
    return {
        "request_id": "high/2026-09-15T00:00:00Z/1",
        "cadence": "high",
        "scheduled_at": "2026-09-15T00:00:00Z",
        "source_id": 1,
        **updates,
    }


@pytest.mark.parametrize("source_id", [True, 0, -1, "1"])
def test_source_id_requires_positive_integer(source_id):
    """受信側でもソースIDを補完・型変換しない。"""
    with pytest.raises(AcquisitionRequestInvalidError):
        acquisition_request_from_message(
            SqsRecord(
                message_id="id", body=json.dumps(message(source_id=source_id))
            ).parse_json()
        )
