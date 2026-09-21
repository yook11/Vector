"""ValidationErrorの変換で、入力値を除き検証エラーの情報を残す。"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticCustomError

from app.log_policy.exceptions.extraction import extract_exception_fields

pytestmark = pytest.mark.unit

_SECRET = "synthetic-row-value"


class TestValidationException:
    """ValidationError の str には input・loc・カスタム文が入る。

    件数と標準分類だけ残す。
    """

    def test_validation_exception_omits_input_and_dynamic_location(self) -> None:
        """件数と標準分類だけ残し、input と入力由来のキー名は出さない。"""

        class Payload(BaseModel):
            values: dict[str, int]

        with pytest.raises(ValidationError) as captured:
            Payload.model_validate({"values": {_SECRET: _SECRET}})
        assert _SECRET in str(captured.value)
        fields = extract_exception_fields(captured.value)
        assert fields is not None
        assert fields["error_class"] == "pydantic_core._pydantic_core.ValidationError"
        assert fields["error_message"] == "Validation failed (1 errors): int_parsing"
        assert "sqlstate" not in fields
        assert _SECRET not in json.dumps(fields)

    def test_validation_custom_message_is_not_forwarded(self) -> None:
        """title / loc / カスタム分類に入力が入っていても custom_error だけ残す。"""
        exc = ValidationError.from_exception_data(
            _SECRET,
            [
                {
                    "type": PydanticCustomError(_SECRET, _SECRET),
                    "loc": (_SECRET,),
                    "input": _SECRET,
                }
            ],
        )
        assert _SECRET in str(exc)
        fields = extract_exception_fields(exc)
        assert fields is not None
        assert fields["error_message"] == "Validation failed (1 errors): custom_error"
        assert _SECRET not in json.dumps(fields)
