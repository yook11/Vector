"""取得依頼の受信契約に固有の不正入力を検証する。"""

import json

import pytest

from app.collection.sources.acquisition_request import AcquisitionRequestInvalidError
from app.lambda_handlers.acquisition.message import (
    AcquisitionMessageJsonInvalidError,
    parse_acquisition_request,
)


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
        parse_acquisition_request(json.dumps(message(source_id=source_id)))


@pytest.mark.parametrize("body", ["{", '{"source_id":1,"source_id":2}', "NaN"])
def test_nonstandard_or_invalid_json_is_rejected(body):
    """曖昧な本文を依頼として受け付けない。"""
    with pytest.raises(
        (AcquisitionMessageJsonInvalidError, AcquisitionRequestInvalidError)
    ):
        parse_acquisition_request(body)
