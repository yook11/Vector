"""個別の制限とログ全体の共有予算超過を分けて検証する。"""

import pytest
from asyncpg import PostgresError
from sqlalchemy.exc import IntegrityError

from app.log_policy import BASE_LOG_RULES, LogPolicy, LogPolicyRules
from app.log_policy.budget import (
    MAX_ITEMS_PER_LOG_EVENT,
    TEXT_LIMIT,
)
from app.log_policy.exceptions import extraction
from app.log_policy.processor import LogPolicyProcessor
from app.shared.errors import ApplicationError

pytestmark = pytest.mark.unit


def _raised_through_frames[E: BaseException](exc: E, frame_count: int) -> E:
    """指定した数の関数呼び出しを通り抜けた状態で、渡した例外を返す。"""

    def fail(depth: int) -> None:
        if depth:
            fail(depth - 1)
        else:
            raise exc

    try:
        fail(frame_count - 2)
    except BaseException as raised:
        if raised is exc:
            return exc
        raise
    raise AssertionError("unreachable")


class TestLocalReplacement:
    """超過した位置だけを置換し、正常な兄弟と前後の項目を残す。"""

    def test_exception_message_at_limit_keeps_redaction(self) -> None:
        """上限ちょうどの例外文は検査後に秘匿して保持する。"""
        suffix = " token=synthetic"
        prefix = "x" * (TEXT_LIMIT - len(suffix))
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": ValueError(prefix + suffix)},
        )
        assert output == {
            "event": "failed",
            "error_class": "builtins.ValueError",
            "error_message": prefix + " token=[redacted:credential]",
            "frames": [],
        }

    def test_over_limit_exception_values_are_replaced_individually(self) -> None:
        """例外由来の型名・例外文・frameの位置情報も、超過した値だけを置換して他を残す。"""
        over_limit = "x" * (TEXT_LIMIT + 1)
        error_type = type(over_limit, (Exception,), {"__module__": "sample"})

        def fail():
            raise error_type(over_limit)

        fail.__code__ = fail.__code__.replace(
            co_filename=over_limit, co_name=over_limit
        )
        with pytest.raises(error_type) as captured:
            fail()
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": captured.value},
        )
        assert output["event"] == "failed"
        assert output["error_class"] == "[limit]"
        assert output["error_message"] == "[limit]"
        assert output["frames"][-1] == {
            "file": "[limit]",
            "function": "[limit]",
            "line": output["frames"][-1]["line"],
        }
        assert output["frames"][0]["function"] == (
            "test_over_limit_exception_values_are_replaced_individually"
        )


class TestWholeLogReplacementByTextBudget:
    """通常項目の名前と値の文字数を合算し、上限を超えたらログ全体を置換する。"""

    def test_processor_text_at_budget_is_not_exceeded(self) -> None:
        """イベント合計16000文字ちょうどで完了すれば通常出力を保持する。"""
        fields = {key: "x" * (TEXT_LIMIT - 1) for key in "abcd"}
        rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset(fields))
        output = LogPolicyProcessor(rules)(None, "info", {**fields})
        assert output == {**fields, "log_policy": rules.policy.value}

    def test_processor_text_above_budget_discards_all_original_fields(self) -> None:
        """合計文字数が一文字でも超過すると先行する正常値も固定ログへ置換する。"""
        fields = {key: "x" * (TEXT_LIMIT - 1) for key in "abc"}
        fields["d"] = "x" * TEXT_LIMIT
        rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset(fields))
        output = LogPolicyProcessor(rules)(None, "info", {**fields})
        assert output == {
            "event": "log_policy_replaced",
            "log_policy": "process_runtime",
            "_policy_replaced_reason": "total_text_length_exceeded",
        }


class TestWholeLogReplacementByItemBudget:
    """除外項目を含む項目と list の要素の件数が上限を超えたらログ全体を置換する。"""

    def test_top_level_count_at_limit_is_not_counted_twice(self) -> None:
        """選別で数えたトップレベル値を再加算せず上限件数ちょうどまで保持する。"""
        names = {f"field_{i}" for i in range(MAX_ITEMS_PER_LOG_EVENT)}
        rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset(names))
        fields = {key: 1 for key in sorted(names)}
        output = LogPolicyProcessor(rules)(None, "info", {**fields})
        assert output == {**fields, "log_policy": rules.policy.value}

    def test_top_level_count_above_limit_replaces_whole_log(self) -> None:
        """許可済みトップレベル項目も上限件数を一件超えるとログ全体を置換する。"""
        fields = {f"field_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT + 1)}
        rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset(fields))
        output = LogPolicyProcessor(rules)(None, "info", fields)
        assert output == {
            "event": "log_policy_replaced",
            "log_policy": "process_runtime",
            "_policy_replaced_reason": "value_count_exceeded",
        }

    def test_container_counts_towards_value_limit(self) -> None:
        """リスト自身と要素の合計が上限件数ちょうどならログを保持する。"""
        items = list(range(MAX_ITEMS_PER_LOG_EVENT - 1))
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "info",
            {"event": items},
        )
        assert output == {"event": items}

    def test_container_above_item_budget_replaces_whole_log(self) -> None:
        """リスト自身を含めて上限件数を一件超える入力はログ全体を置換する。"""
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "info",
            {"event": ["safe"] * MAX_ITEMS_PER_LOG_EVENT},
        )
        assert output == {
            "event": "log_policy_replaced",
            "_policy_replaced_reason": "value_count_exceeded",
        }

    def test_top_level_selection_and_nested_values_share_count_limit(self) -> None:
        """未登録項目の検査も値の走査と同じ件数の予算を消費する。"""
        payload = list(range(6))
        # event・payload自身・payloadの要素と合わせて上限を1件だけ超える数にする。
        unknown_count = MAX_ITEMS_PER_LOG_EVENT + 1 - 2 - len(payload)
        rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset({"payload"}))
        output = LogPolicyProcessor(rules)(
            None,
            "info",
            {
                "event": "failed",
                **{f"unknown_{index}": 0 for index in range(unknown_count)},
                "payload": payload,
            },
        )
        assert output == {
            "event": "log_policy_replaced",
            "log_policy": "process_runtime",
            "_policy_replaced_reason": "value_count_exceeded",
        }


class TestBudgetOverflowReason:
    """複数の共有予算を同時に超えうる入力で、どの理由を出力するか。"""

    def test_top_level_count_overflow_precedes_value_text_overflow(
        self,
    ) -> None:
        """トップレベルの選別で件数上限を超えたら、値の文字数超過より先に通知する。"""
        rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset({"payload"}))
        prepared_event = LogPolicyProcessor(rules)(
            None,
            "info",
            {
                "payload": ["x" * TEXT_LIMIT] * 4,
                **{f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT)},
            },
        )
        assert prepared_event == {
            "event": "log_policy_replaced",
            "log_policy": "process_runtime",
            "_policy_replaced_reason": "value_count_exceeded",
        }


class TestSqlDiagnosticBudgets:
    """SQL診断と原因連鎖は、例外側の上限で制限する。"""

    def test_diagnostic_name_at_text_limit_is_preserved(self) -> None:
        """単一文字列の上限ちょうどの診断名は失われない。"""
        exc = PostgresError.new({"C": "23505", "M": "duplicate", "n": "x" * TEXT_LIMIT})
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": exc},
        )
        assert output["error_details"]["constraint_name"] == "x" * TEXT_LIMIT

    def test_cause_diagnostics_beyond_exception_text_total_are_replaced(self) -> None:
        """各診断属性が単独上限内でも、例外側の文字数の合計に収まらない値だけを置換する。"""
        driver = PostgresError.new(
            {
                "C": "23505",
                "M": "duplicate",
                "s": "x" * TEXT_LIMIT,
                "t": "x" * TEXT_LIMIT,
                "c": "x" * TEXT_LIMIT,
                "n": "x" * TEXT_LIMIT,
            }
        )
        outer = RuntimeError("wrapped")
        outer.__cause__ = IntegrityError("INSERT ...", (), driver)
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": outer},
        )
        details = output["related_exceptions"][0]["exception"]["error_details"]
        assert output["event"] == "failed"
        assert [
            details["schema_name"],
            details["table_name"],
            details["column_name"],
            details["constraint_name"],
        ] == ["x" * TEXT_LIMIT, "x" * TEXT_LIMIT, "x" * TEXT_LIMIT, "[limit]"]

    def test_long_diagnostic_attribute_preserves_other_fields(self) -> None:
        """一つの診断名が長すぎても兄弟のSQLSTATEを残す。"""
        exc = IntegrityError(
            "INSERT ...",
            (),
            PostgresError.new(
                {"C": "23505", "M": "duplicate", "n": "x" * (TEXT_LIMIT + 1)}
            ),
        )
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": exc},
        )
        assert output["error_details"] == {
            "kind": "postgresql",
            "sqlstate": "23505",
            "constraint_name": "[limit]",
        }


class TestExceptionBudget:
    """例外の出力は、通常の項目と別の予算で検査する。"""

    def test_chained_exception_frames_do_not_replace_normal_fields(self) -> None:
        """連鎖した例外のframesが多くても、通常の項目を残したまま出力する。"""
        # frameだけで通常の項目と共有していた件数の予算を使い切る量にする。
        outer_frames = extraction.FRAME_TOTAL_LIMIT // 2
        cause_frames = extraction.FRAME_TOTAL_LIMIT - outer_frames
        outer = _raised_through_frames(ValueError("fetch failed"), outer_frames)
        outer.__cause__ = _raised_through_frames(OSError("timeout"), cause_frames)
        rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset({"resource"}))

        output = LogPolicyProcessor(rules)(
            None,
            "error",
            {"event": "fetch_failed", "resource": "articles", "exc_info": outer},
        )

        assert output["event"] == "fetch_failed"
        assert output["resource"] == "articles"
        assert len(output["frames"]) == outer_frames
        assert len(output["related_exceptions"][0]["exception"]["frames"]) == (
            cause_frames
        )

    def test_exception_output_at_its_limits_passes_inspection_unchanged(self) -> None:
        """例外側の上限いっぱいの出力は、検査を通っても置き換えられない。"""
        # 件数・frame数・診断情報の項目数が例外側の上限を超える連鎖にする。
        # 原因文は後ろほど短くし、長い原因文が入らなかった残りの文字数まで埋める。
        text_limit = extraction.TEXT_LENGTH_LIMIT
        message_lengths = [text_limit] * 3 + [
            text_limit // 2**shift for shift in range(1, extraction.EXCEPTION_LIMIT - 1)
        ]
        chain = [
            _raised_through_frames(
                ApplicationError(
                    "m" * length,
                    details={
                        f"item_{index}": "value"
                        for index in range(extraction.DETAILS_ITEM_LIMIT)
                    },
                ),
                extraction.FRAME_TOTAL_LIMIT // 2,
            )
            for length in message_lengths
        ]
        for parent, cause in zip(chain, chain[1:]):
            parent.__cause__ = cause
        exception_output = extraction.extract_exception_log(chain[0])

        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": chain[0]},
        )

        assert output == {"event": "failed", **exception_output}
