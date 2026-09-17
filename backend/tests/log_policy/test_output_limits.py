"""サニタイズと分離した出力上限が、秘匿後に適用されることを検証する。"""

import pytest

from app.log_policy.exception_messages import extract_exception_message
from app.log_policy.rules import BASE_DENY
from app.log_policy.safe_exception_log import extract_safe_exception_fields
from app.log_policy.sanitize import TEXT_LIMIT
from app.log_policy.value_protection import ValueProtector

pytestmark = pytest.mark.unit


def test_secret_at_truncation_boundary_leaves_no_fragment() -> None:
    """通常値の出力は、長さ制限より先に秘密値を全体置換する。"""
    key = "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
    text = "x" * (TEXT_LIMIT - 10) + key
    protected = ValueProtector(BASE_DENY).protect_fields({"sample": text})
    assert protected["sample"] == "x" * (TEXT_LIMIT - 10) + "AIza***"


def test_long_text_is_truncated_to_limit() -> None:
    """出力時に通常の長文を先頭 TEXT_LIMIT 文字に制限する。"""
    text = "y" * (TEXT_LIMIT * 3)
    assert ValueProtector(BASE_DENY).protect_fields({"sample": text}) == {
        "sample": text[:TEXT_LIMIT]
    }


def test_nested_key_is_protected_before_truncation() -> None:
    """辞書のキー名も秘匿してから文字数制限を適用する。"""
    key = "x" * (TEXT_LIMIT - 5) + " password='synthetic private'"
    output = ValueProtector(BASE_DENY).protect_fields({"payload": {key: 1}})
    assert output["payload"] == {
        ("x" * (TEXT_LIMIT - 5) + " password=***")[:TEXT_LIMIT]: 1
    }


def test_exception_message_is_protected_before_truncation() -> None:
    """例外文は抽出後に全文を秘匿し、その結果に文字数制限を適用する。"""
    message = "x" * (TEXT_LIMIT - 5) + " password='synthetic private'"
    fields = extract_safe_exception_fields(ValueError(message))
    assert fields is not None
    assert (
        fields["error_message"]
        == ("x" * (TEXT_LIMIT - 5) + " password=***")[:TEXT_LIMIT]
    )


def test_exception_extraction_does_not_apply_common_string_policy() -> None:
    """例外固有の抽出では共通の文字列秘匿や文字数制限を実行しない。"""
    message = "x" * TEXT_LIMIT + " password='synthetic private'"
    assert extract_exception_message(ValueError(message)) == message


def test_exception_class_name_keeps_protection_and_limit() -> None:
    """例外型名の保護と文字数制限も組み立て側で維持する。"""
    name = "x" * (TEXT_LIMIT - 10) + " password='synthetic private'"
    error_type = type(name, (Exception,), {"__module__": "sample"})
    fields = extract_safe_exception_fields(error_type())
    assert fields is not None
    assert (
        fields["error_class"]
        == ("sample." + "x" * (TEXT_LIMIT - 10) + " password=***")[:TEXT_LIMIT]
    )


def test_frame_metadata_keeps_protection_and_limit() -> None:
    """frame のファイル名と関数名も秘匿後の長さに制限する。"""

    def fail():
        raise ValueError("failed")

    name = "x" * (TEXT_LIMIT - 10) + " password='synthetic private'"
    fail.__code__ = fail.__code__.replace(co_filename=name, co_name=name)
    with pytest.raises(ValueError) as captured:
        fail()
    fields = extract_safe_exception_fields(captured.value)
    assert fields is not None
    frame = fields["frames"][-1]
    expected = ("x" * (TEXT_LIMIT - 10) + " password=***")[:TEXT_LIMIT]
    assert frame["file"] == expected
    assert frame["function"] == expected
