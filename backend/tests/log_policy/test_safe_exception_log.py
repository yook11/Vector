"""安全な例外ログの契約: 例外の形ごとに入力値を除き、型・原因分類・発生位置を残す。"""

from __future__ import annotations

import json
import sys

import pytest
import structlog
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticCustomError
from sqlalchemy.exc import IntegrityError

from app.log_policy import BASE_LOG_RULES, PolicyLogger
from app.log_policy.budget import TEXT_LIMIT
from app.log_policy.processor import LogPolicyProcessor
from app.log_policy.safe_exception_log import FRAME_LIMIT, extract_exception_fields

pytestmark = pytest.mark.unit

_SECRET = "synthetic-row-value"


def test_exception_extraction_leaves_sanitization_to_common_preparation() -> None:
    """例外抽出だけの入口は共通サニタイズも文字数制限も行わず、原文のフィールドを返す。"""
    message = (
        "x" * TEXT_LIMIT + " request with sk-proj-abcdef0123456789ABCDEFxyz failed"
    )
    fields = extract_exception_fields(ValueError(message))
    assert fields == {
        "error_class": "builtins.ValueError",
        "error_message": message,
        "frames": [],
    }


class TestSqlException:
    """SQLAlchemy 例外は str(exc) と params に行の値が入る。

    型・制約名・SQLSTATE は残し、値は出さない。
    """

    @staticmethod
    def _raise_integrity_error() -> None:
        raise IntegrityError(
            "INSERT INTO t (a) VALUES (%s)",
            (_SECRET,),
            Exception('duplicate key value violates unique constraint "t_a_key"'),
            hide_parameters=False,
        )

    @staticmethod
    def _capture_exc_info():
        try:
            TestSqlException._raise_integrity_error()
        except IntegrityError:
            return sys.exc_info()
        raise AssertionError("unreachable")

    def test_sql_and_parameters_segments_are_removed(self) -> None:
        """`[SQL: …]` と `[parameters: …]` は出さず、型と primary は残す。"""
        exc_info = self._capture_exc_info()
        raw = str(exc_info[1])
        assert "[SQL:" in raw
        assert "[parameters:" in raw
        assert _SECRET in raw
        fields = extract_exception_fields(exc_info)
        assert fields is not None
        assert fields["error_class"] == "sqlalchemy.exc.IntegrityError"
        assert fields["error_message"] == (
            "(builtins.Exception) duplicate key value violates "
            'unique constraint "t_a_key"'
        )

    @pytest.mark.parametrize(
        "supplement",
        [
            pytest.param(f"DETAIL: Key (a)=({_SECRET}) exists.", id="detail"),
            pytest.param(f"HINT: Use {_SECRET}", id="hint"),
            pytest.param(f"CONTEXT: {_SECRET}", id="context"),
            pytest.param(f"QUERY: SELECT {_SECRET}", id="query"),
            pytest.param(f"STATEMENT: {_SECRET}", id="statement"),
            pytest.param(f"LINE 12: {_SECRET}", id="line"),
        ],
    )
    def test_sql_supplement_is_stripped_and_row_value_removed(
        self, supplement: str
    ) -> None:
        """DETAIL / HINT 以降の補足は切り、制約名は残し行の値は出さない。"""
        orig = Exception(
            f'duplicate key violates unique constraint "t_key"\n{supplement}'
        )
        exc = IntegrityError("INSERT ...", (_SECRET,), orig)
        assert _SECRET in exc.args[0]
        fields = extract_exception_fields(exc)
        assert fields is not None
        assert fields["error_message"] == (
            '(builtins.Exception) duplicate key violates unique constraint "t_key"'
        )
        assert _SECRET not in json.dumps(fields)

    def test_sql_primary_message_does_not_expose_parameter(self) -> None:
        """primary message に埋め込まれた bind 値は伏せ、原因分類の文言は残す。"""
        exc = IntegrityError(
            "INSERT ...",
            (_SECRET,),
            Exception(f'invalid input syntax for type integer: "{_SECRET}"'),
        )
        assert _SECRET in exc.args[0]
        fields = extract_exception_fields(exc)
        assert fields is not None
        assert fields["error_message"] == (
            '(builtins.Exception) invalid input syntax for type integer: "***"'
        )

    def test_sqlstate_survives_parameter_removal(self) -> None:
        """SQLSTATE は残し、パラメータは message に残さない。"""

        class DriverError(Exception):
            sqlstate = "23505"

        exc = IntegrityError("INSERT ...", (_SECRET,), DriverError("duplicate key"))
        fields = extract_exception_fields(exc)
        assert fields is not None
        assert fields["sqlstate"] == "23505"
        assert "duplicate key" in fields["error_message"]
        assert _SECRET not in json.dumps(fields)

    def test_missing_sqlstate_is_omitted(self) -> None:
        """driverに診断コードがない場合はsqlstateフィールドを作らない。"""
        exc = IntegrityError("INSERT ...", (), Exception("duplicate key"))
        fields = extract_exception_fields(exc)
        assert fields is not None
        assert "sqlstate" not in fields

    @pytest.mark.parametrize("state", ["2350", "235050", "42p01", 23505])
    def test_invalid_sqlstate_is_omitted(self, state) -> None:
        """英大文字・数字5文字でない診断コードはログへ追加しない。"""

        class DriverError(Exception):
            sqlstate = state

        exc = IntegrityError("INSERT ...", (), DriverError("duplicate key"))
        fields = extract_exception_fields(exc)
        assert fields is not None
        assert "sqlstate" not in fields

    def test_pgcode_is_output_as_sqlstate(self) -> None:
        """driverがpgcodeで返す診断コードもsqlstateとして組み立てる。"""

        class DriverError(Exception):
            pgcode = "23505"

        exc = IntegrityError("INSERT ...", (), DriverError("duplicate key"))
        fields = extract_exception_fields(exc)
        assert fields is not None
        assert fields["sqlstate"] == "23505"

    def test_sqlstate_extraction_failure_uses_fixed_message(self) -> None:
        """SQLSTATEの取得が失敗しても途中の原因文や診断コードを出さない。"""

        class DriverError(Exception):
            @property
            def sqlstate(self):
                raise RuntimeError(_SECRET)

        exc = IntegrityError("INSERT ...", (), DriverError("duplicate key"))
        fields = extract_exception_fields(exc)
        assert fields == {
            "error_class": "sqlalchemy.exc.IntegrityError",
            "error_message": "[exception message unavailable]",
            "frames": [],
        }

    def test_cyclic_sql_parameters_use_fixed_message(self) -> None:
        """パラメータが循環しても原文へ戻らず、固定文だけを残す。"""
        params = [_SECRET]
        params.append(params)
        exc = IntegrityError("INSERT ...", params, Exception(_SECRET))
        assert _SECRET in exc.args[0]
        fields = extract_exception_fields(exc)
        assert fields is not None
        assert fields["error_message"] == (
            "SQL error (parameters exceeded inspection limit)"
        )
        assert _SECRET not in json.dumps(fields)


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


class TestBrokenException:
    """例外の文字列化が失敗しても、型と発生箇所は残し原文は出さない。"""

    def test_broken_exception_string_keeps_type_and_frame(self) -> None:
        """`__str__` が失敗しても型と発生箇所を残し、message は固定文にする。"""

        class BrokenError(Exception):
            def __str__(self):
                raise RuntimeError(_SECRET)

        try:
            raise BrokenError()
        except BrokenError as exc:
            fields = extract_exception_fields(exc)
        assert fields is not None
        assert fields["error_class"].endswith("BrokenError")
        assert fields["error_message"] == "[exception message unavailable]"
        assert (
            fields["frames"][-1]["function"]
            == "test_broken_exception_string_keeps_type_and_frame"
        )
        assert _SECRET not in json.dumps(fields)


class TestExcInfo:
    """structlog の exc_info を型・原因文・frame だけに正規化し、不正な値は無視する。"""

    def test_frames_carry_only_file_function_line(self) -> None:
        """frame は file / function / line のみで、locals やソース行は含めない。"""

        def _raise() -> None:
            raise ValueError(_SECRET)

        try:
            _raise()
        except ValueError:
            fields = extract_exception_fields(sys.exc_info())
        assert fields is not None
        innermost = fields["frames"][-1]
        assert set(innermost) == {"file", "function", "line"}
        assert innermost["function"] == "_raise"
        assert _SECRET not in repr(fields["frames"])

    def test_exc_info_true_resolves_current_exception(self) -> None:
        """`exc_info=True` は現在処理中の例外へ解決される。"""
        try:
            raise ValueError("boom")
        except ValueError:
            fields = extract_exception_fields(True)
        assert fields is not None
        assert fields["error_class"] == "builtins.ValueError"
        assert fields["error_message"] == "boom"

    def test_exc_info_exception_instance_is_described(self) -> None:
        """例外インスタンスをそのまま渡す形式も同じ構造になる。"""
        try:
            raise RuntimeError("instance form")
        except RuntimeError as exc:
            fields = extract_exception_fields(exc)
        assert fields is not None
        assert fields["error_class"] == "builtins.RuntimeError"
        assert fields["error_message"] == "instance form"
        assert fields["frames"][-1]["function"].startswith("test_exc_info_exception")

    @pytest.mark.parametrize("value", [None, False], ids=["none", "false"])
    def test_exc_info_absent_yields_nothing(self, value) -> None:
        """exc_info が無い / False のログには例外フィールドを足さない。"""
        assert extract_exception_fields(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param((str, "raw exception", None), id="type_mismatch"),
            pytest.param(
                (ValueError, ValueError(), "bad traceback"), id="non_traceback"
            ),
        ],
    )
    def test_invalid_exc_info_is_ignored(self, value) -> None:
        """型が合わない tuple は例外フィールドを作らない。"""
        assert extract_exception_fields(value) is None

    def test_invalid_exc_info_is_not_forwarded(self) -> None:
        """processor は不正な exc_info を無視し、生値もキーも出さない。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "exc_info": (str, _SECRET, None)},
        )
        assert "exc_info" not in output
        assert "error_class" not in output
        assert _SECRET not in json.dumps(output)

    def test_frame_count_at_limit_is_preserved(self) -> None:
        """frame数が上限ちょうどなら全件を抽出する。"""

        def fail(depth):
            if depth:
                fail(depth - 1)
            else:
                raise ValueError("failed")

        with pytest.raises(ValueError) as captured:
            fail(FRAME_LIMIT - 2)
        fields = extract_exception_fields(captured.value)
        assert fields is not None
        assert len(fields["frames"]) == FRAME_LIMIT
        assert fields["frames"][-1]["function"] == "fail"

    def test_frame_count_over_limit_replaces_whole_frames_field(self) -> None:
        """frame数が上限を超える場合は末尾への切り詰めも行わず、frames全体を固定マーカーにする。"""

        def fail(depth):
            if depth:
                fail(depth - 1)
            else:
                raise ValueError("failed")

        with pytest.raises(ValueError) as captured:
            fail(FRAME_LIMIT - 1)
        assert extract_exception_fields(captured.value) == {
            "error_class": "builtins.ValueError",
            "error_message": "failed",
            "frames": "[limit]",
        }
