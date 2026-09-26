"""PostgreSQL診断の取得元・許可属性と、原因ノードへの配置を検証する。"""

import json

import pytest
from asyncpg import PostgresError
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError

from app.db.translate import translate_database_error
from app.log_policy.exceptions.extraction import extract_exception_fields
from app.log_policy.exceptions.sql import SQL_EXCEPTION_LIMIT, extract_sql_error_details

pytestmark = pytest.mark.unit


class TestPostgresDetails:
    def test_driver_attributes_are_selected_without_arbitrary_data(self) -> None:
        """プロトコルの診断属性だけを選び、行データや未知属性を転記しない。"""
        driver = PostgresError.new(
            {
                "C": "23505",
                "M": "duplicate key",
                "s": "public",
                "t": "articles",
                "c": "external_id",
                "n": "articles_external_id_key",
                "d": "uuid",
                "D": "synthetic-row",
                "H": "synthetic-hint",
                "W": "synthetic-context",
            }
        )
        driver.extra = {"secret": "synthetic-secret"}
        assert extract_sql_error_details(driver) == {
            "kind": "postgresql",
            "sqlstate": "23505",
            "schema_name": "public",
            "table_name": "articles",
            "column_name": "external_id",
            "constraint_name": "articles_external_id_key",
            "data_type_name": "uuid",
        }

    def test_sqlalchemy_adapter_resolves_native_source(self) -> None:
        """SQLAlchemyの変換例外を越えて、同じ元例外の診断属性を取得する。"""
        driver = PostgresError.new(
            {"C": "23505", "M": "duplicate", "n": "articles_key"}
        )
        adapter = AsyncAdapt_asyncpg_dbapi.IntegrityError("duplicate")
        adapter.__cause__ = driver
        adapter.table_name = "unrelated_table"
        exc = IntegrityError("INSERT ...", (), adapter)
        assert extract_sql_error_details(exc) == {
            "kind": "postgresql",
            "sqlstate": "23505",
            "constraint_name": "articles_key",
        }

    def test_adapter_sqlstate_survives_missing_native_source(self) -> None:
        """元例外がない既知のadapterでも、取得済みの診断コードは保持する。"""
        adapter = AsyncAdapt_asyncpg_dbapi.IntegrityError("duplicate")
        adapter.sqlstate = "23505"
        assert extract_sql_error_details(IntegrityError("INSERT ...", (), adapter)) == {
            "kind": "postgresql",
            "sqlstate": "23505",
        }

    def test_unknown_driver_with_sqlstate_is_not_postgres(self) -> None:
        """同名属性があるだけの未知例外をPostgreSQLと判定しない。"""
        driver = Exception("unknown")
        driver.sqlstate = "23505"
        assert (
            extract_sql_error_details(IntegrityError("INSERT ...", (), driver)) is None
        )

    @pytest.mark.parametrize("value", [None, "", 42, True, b"table"])
    def test_invalid_identifier_is_omitted(self, value) -> None:
        """空または組み込み文字列以外の対象名は省略する。"""
        driver = PostgresError("failed")
        driver.table_name = value
        assert extract_sql_error_details(driver) is None

    def test_string_subclass_identifier_is_omitted(self) -> None:
        """独自処理を持ち得る文字列サブクラスの対象名は採用しない。"""

        class Identifier(str):
            pass

        driver = PostgresError("failed")
        driver.table_name = Identifier("articles")
        assert extract_sql_error_details(driver) is None

    def test_broken_attribute_preserves_other_diagnostics(self) -> None:
        """一属性の取得失敗は他の正常な診断属性へ波及しない。"""

        class BrokenConstraint(PostgresError):
            table_name = "articles"

            @property
            def constraint_name(self):
                raise RuntimeError("synthetic-secret")

        driver = BrokenConstraint("failed")
        driver.sqlstate = "23505"
        assert extract_sql_error_details(driver) == {
            "kind": "postgresql",
            "sqlstate": "23505",
            "table_name": "articles",
        }

    def test_missing_sqlstate_is_omitted(self) -> None:
        """driverに診断コードがない場合はsqlstateフィールドを作らない。"""
        exc = IntegrityError("INSERT ...", (), PostgresError("duplicate key"))
        details = extract_sql_error_details(exc)
        assert details is None

    @pytest.mark.parametrize("state", ["2350", "235050", "42p01", 23505])
    def test_invalid_sqlstate_is_omitted(self, state) -> None:
        """英大文字・数字5文字でない診断コードはログへ追加しない。"""

        driver = PostgresError("duplicate key")
        driver.sqlstate = state
        exc = IntegrityError("INSERT ...", (), driver)
        details = extract_sql_error_details(exc)
        assert details is None

    def test_pgcode_is_output_as_sqlstate(self) -> None:
        """driverがpgcodeで返す診断コードもsqlstateとして組み立てる。"""

        class DriverError(PostgresError):
            pgcode = "23505"

        exc = IntegrityError("INSERT ...", (), DriverError("duplicate key"))
        details = extract_sql_error_details(exc)
        assert details == {"kind": "postgresql", "sqlstate": "23505"}


class TestSqlSourceTraversal:
    def test_source_at_search_limit_is_found(self) -> None:
        """SQL内部の探索上限ちょうどにある元例外を採用する。"""
        current = PostgresError.new({"C": "23505", "M": "duplicate"})
        for _ in range(SQL_EXCEPTION_LIMIT - 1):
            wrapper = RuntimeError("wrapped")
            wrapper.__cause__ = current
            current = wrapper
        assert extract_sql_error_details(IntegrityError("INSERT ...", (), current)) == {
            "kind": "postgresql",
            "sqlstate": "23505",
        }

    def test_source_beyond_search_limit_is_not_read(self) -> None:
        """SQL内部の探索上限を越えた元例外を参照しない。"""
        current = PostgresError.new({"C": "23505", "M": "duplicate"})
        for _ in range(SQL_EXCEPTION_LIMIT):
            wrapper = RuntimeError("wrapped")
            wrapper.__cause__ = current
            current = wrapper
        assert (
            extract_sql_error_details(IntegrityError("INSERT ...", (), current)) is None
        )

    def test_cyclic_source_is_not_followed_forever(self) -> None:
        """循環するSQL内部の例外探索は停止する。"""
        driver = RuntimeError("wrapped")
        driver.__cause__ = driver
        assert (
            extract_sql_error_details(IntegrityError("INSERT ...", (), driver)) is None
        )

    def test_suppressed_driver_context_is_not_used(self) -> None:
        """明示的に抑制されたdriverのcontextから診断を取得しない。"""
        adapter = AsyncAdapt_asyncpg_dbapi.Error("wrapped")
        adapter.__context__ = PostgresError.new({"C": "23505", "M": "duplicate"})
        adapter.__suppress_context__ = True
        assert (
            extract_sql_error_details(IntegrityError("INSERT ...", (), adapter)) is None
        )


class TestSqlNodeAssembly:
    """アプリ用例外との診断分離とSQL内部原因の集約を確認する。"""

    def test_explicit_cause_keeps_its_own_fields(self) -> None:
        """外側の例外へ内側の診断属性を引き上げない。"""
        sql_error = IntegrityError(
            "INSERT ...", (), PostgresError.new({"C": "23505", "M": "duplicate"})
        )
        outer = translate_database_error(sql_error)
        outer.__cause__ = sql_error
        fields = extract_exception_fields(outer)
        assert fields["error_class"] == "app.db.errors.DatabaseConstraintError"
        assert "error_details" not in fields
        assert (
            fields["related_exceptions"][0]["exception"]["error_class"]
            == "sqlalchemy.exc.IntegrityError"
        )
        assert fields["related_exceptions"][0]["exception"]["error_details"] == {
            "kind": "postgresql",
            "sqlstate": "23505",
        }

    def test_driver_chain_is_not_rendered_as_unprotected_causes(self) -> None:
        """SQL内部の同一失敗を集約し、driverのDETAILを別ノードから再出力しない。"""
        driver = PostgresError.new(
            {
                "C": "23505",
                "M": "duplicate",
                "D": "synthetic-private-row",
                "n": "articles_key",
            }
        )
        adapter = AsyncAdapt_asyncpg_dbapi.IntegrityError(str(driver))
        adapter.__cause__ = driver
        sql_error = IntegrityError(
            "synthetic-private-query", ("synthetic-private-row",), adapter
        )
        sql_error.__cause__ = adapter
        fields = extract_exception_fields(sql_error)
        assert fields["error_details"]["constraint_name"] == "articles_key"
        assert "related_exceptions" not in fields
        assert "synthetic-private" not in json.dumps(fields)

    def test_unknown_sql_driver_cannot_bypass_parameter_protection(self) -> None:
        """未対応driverのcauseもSQLノードから原文で再出力しない。"""
        driver = ValueError("synthetic-private-parameter")
        exc = IntegrityError("INSERT ...", ("synthetic-private-parameter",), driver)
        exc.__cause__ = driver
        fields = extract_exception_fields(exc)
        assert "related_exceptions" not in fields
        assert "synthetic-private-parameter" not in json.dumps(fields)
