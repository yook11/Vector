"""例外1件をログ用の情報へ変換する振る舞いを検証する。"""

from dataclasses import asdict

import pytest

from app.log_policy.exceptions.conversion import convert_exception

pytestmark = pytest.mark.unit


def test_normal_exception_does_not_copy_extra_attributes() -> None:
    """通常例外のメッセージを取り出し、独自属性は変換結果に追加しない。"""

    class ErrorWithExtra(Exception):
        def __init__(self) -> None:
            super().__init__("operation failed")
            self.extra = {"private_value": "must not be copied"}

    exc = ErrorWithExtra()

    result = convert_exception(exc)

    assert asdict(result) == {
        "message": "operation failed",
        "error_details": None,
        "cause_is_aggregated": False,
    }


def test_failed_string_conversion_uses_unavailable_message() -> None:
    """例外を文字列に変換できなかったら、原因文を固定メッセージにする。"""

    class BrokenStringError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("internal conversion failure")

    exc = BrokenStringError()

    result = convert_exception(exc)

    assert asdict(result) == {
        "message": "[exception message unavailable]",
        "error_details": None,
        "cause_is_aggregated": False,
    }
