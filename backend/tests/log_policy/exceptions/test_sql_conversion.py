"""SQL例外1件の原因文の保護と、診断属性を保持する変換を検証する。"""

import json
from dataclasses import asdict
from uuid import UUID

import pytest
from asyncpg import PostgresError
from sqlalchemy.exc import IntegrityError

from app.log_policy.exceptions.conversion import convert_exception

pytestmark = pytest.mark.unit

_SECRET = "synthetic-row-value"


class TestSqlConversion:
    """SQL本文やパラメータを出さず、ログ用の原因情報へ変換する。"""

    def test_sql_and_parameters_segments_are_removed(self) -> None:
        """SQL本文とパラメータを除いた原因文を返し、内部原因は集約済みとする。"""
        exc = IntegrityError(
            "INSERT INTO t (a) VALUES (%s)",
            (_SECRET,),
            Exception('duplicate key value violates unique constraint "t_a_key"'),
            hide_parameters=False,
        )
        raw = str(exc)
        assert "[SQL:" in raw
        assert "[parameters:" in raw
        assert _SECRET in raw

        result = convert_exception(exc)

        assert asdict(result) == {
            "message": (
                "(builtins.Exception) duplicate key value violates "
                'unique constraint "t_a_key"'
            ),
            "error_details": None,
            "cause_is_aggregated": True,
        }

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
        result = convert_exception(exc)
        assert result.message == (
            '(builtins.Exception) duplicate key violates unique constraint "t_key"'
        )
        assert _SECRET not in json.dumps(asdict(result))

    def test_sql_primary_message_does_not_expose_parameter(self) -> None:
        """primary message に埋め込まれた bind 値は伏せ、原因分類の文言は残す。"""
        exc = IntegrityError(
            "INSERT ...",
            (_SECRET,),
            Exception(f'invalid input syntax for type integer: "{_SECRET}"'),
        )
        assert _SECRET in exc.args[0]
        result = convert_exception(exc)
        assert result.message == (
            '(builtins.Exception) invalid input syntax for type integer: "***"'
        )

    def test_sqlstate_survives_parameter_removal(self) -> None:
        """SQLSTATE は残し、パラメータは message に残さない。"""

        driver = PostgresError.new({"C": "23505", "M": "duplicate key"})
        exc = IntegrityError("INSERT ...", (_SECRET,), driver)
        result = convert_exception(exc)
        assert result.error_details == {"kind": "postgresql", "sqlstate": "23505"}
        assert "duplicate key" in result.message
        assert _SECRET not in json.dumps(asdict(result))

    def test_sqlstate_extraction_failure_preserves_message(self) -> None:
        """SQLSTATEの取得失敗で保護済みの原因文まで捨てない。"""

        class DriverError(PostgresError):
            @property
            def sqlstate(self):
                raise RuntimeError(_SECRET)

        exc = IntegrityError("INSERT ...", (), DriverError("duplicate key"))
        result = convert_exception(exc)
        assert result.message.endswith("duplicate key")
        assert result.error_details is None
        assert _SECRET not in json.dumps(asdict(result))

    def test_cyclic_sql_parameters_use_fixed_message(self) -> None:
        """パラメータが循環しても原文へ戻らず、固定文だけを残す。"""
        params = [_SECRET]
        params.append(params)
        exc = IntegrityError("INSERT ...", params, Exception(_SECRET))
        assert _SECRET in exc.args[0]
        result = convert_exception(exc)
        assert result.message == ("SQL error (parameters exceeded inspection limit)")
        assert _SECRET not in json.dumps(asdict(result))


class TestSqlMessageIndependence:
    def test_short_parameter_does_not_mask_constraint_attribute(self) -> None:
        """短いbind値に一致しても制約名の診断属性を削らない。"""
        driver = PostgresError.new(
            {"C": "23505", "M": 'duplicate "articles_key"', "n": "articles_key"}
        )
        result = convert_exception(IntegrityError("INSERT ...", ("a",), driver))
        assert result.error_details["constraint_name"] == "articles_key"
        assert '"***rticles_key"' in result.message

    def test_uuid_parameter_does_not_discard_diagnostics(self) -> None:
        """原因文を保護できないUUIDパラメータがあっても診断属性は残る。"""
        driver = PostgresError.new({"C": "23502", "M": "null value", "c": "title"})
        result = convert_exception(IntegrityError("INSERT ...", (UUID(int=1),), driver))
        assert result.message == "SQL error (unsupported parameter type)"
        assert result.error_details == {
            "kind": "postgresql",
            "sqlstate": "23502",
            "column_name": "title",
        }

    def test_direct_driver_omits_message_without_parameter_context(self) -> None:
        """パラメータ保護文脈がないdriverの原文は出さず、診断属性だけ残す。"""
        result = convert_exception(
            PostgresError.new({"C": "22P02", "M": "synthetic-private-value"})
        )
        assert asdict(result) == {
            "message": "[exception message omitted]",
            "error_details": {"kind": "postgresql", "sqlstate": "22P02"},
            "cause_is_aggregated": True,
        }

    def test_message_failure_does_not_discard_diagnostics(self) -> None:
        """SQL原因文の抽出が例外になっても診断属性を保持する。"""

        class BrokenParams(IntegrityError):
            def __getattribute__(self, name):
                if name == "params":
                    raise RuntimeError("synthetic-private-value")
                return super().__getattribute__(name)

        result = convert_exception(
            BrokenParams(
                "INSERT ...", (), PostgresError.new({"C": "23505", "M": "duplicate"})
            )
        )
        assert result.message == "[exception message unavailable]"
        assert result.error_details == {"kind": "postgresql", "sqlstate": "23505"}
