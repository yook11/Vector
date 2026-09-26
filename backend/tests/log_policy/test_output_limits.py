"""個別の制限とログ全体の共有予算超過を分けて検証する。"""

from typing import Any

import pytest
import structlog
from asyncpg import PostgresError
from sqlalchemy.exc import IntegrityError

from app.log_policy import BASE_LOG_RULES, LogPolicy, LogPolicyRules, PolicyLogger
from app.log_policy.budget import (
    EVENT_TEXT_LIMIT,
    MAX_ITEMS_PER_LOG_EVENT,
    TEXT_LIMIT,
)
from app.log_policy.processor import LogPolicyProcessor

pytestmark = pytest.mark.unit


class TestLocalReplacement:
    """超過した位置だけを置換し、正常な兄弟と前後の項目を残す。"""

    def test_field_limit_preserves_earlier_and_later_normal_fields(self) -> None:
        """単一項目の超過は前後の正常なトップレベル項目を巻き込まない。"""
        fields = {"event": "failed", "payload": ["x" * (TEXT_LIMIT + 1)], "attempt": 2}
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset(fields))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()), "info", fields
        )
        assert output == {
            "event": "failed",
            "payload": ["[limit]"],
            "attempt": 2,
            "log_policy": "infrastructure",
        }

    def test_nested_key_at_limit_is_redacted_without_truncation(self) -> None:
        """上限内の辞書キーは全文に情報漏洩防止を適用して残す。"""
        suffix = " password='synthetic private'"
        key = "x" * (TEXT_LIMIT - len(suffix)) + suffix
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": {key: 1}},
        )
        assert output == {
            "event": {
                "x" * (TEXT_LIMIT - len(suffix)) + " password=[redacted:credential]": 1
            }
        }

    def test_integer_at_bit_limit_is_preserved(self) -> None:
        """4096bitちょうどの整数は正常な別項目とともにログへ残す。"""
        fields = {"event": (1 << 4096) - 1, "logger": "test"}
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", fields
        )
        assert output == fields

    def test_integer_above_bit_limit_is_replaced(self) -> None:
        """4097bitの整数だけを置換し、正常な別項目はログへ残す。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": 1 << 4096, "logger": "test"},
        )
        assert output == {"event": "[limit]", "logger": "test"}

    def test_nested_large_integer_preserves_siblings(self) -> None:
        """ネスト内の巨大整数だけを置換し、同じ辞書の正常な値は保持する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": {"too_large": 1 << 4096, "count": 3}},
        )
        assert output == {"event": {"too_large": "[limit]", "count": 3}}

    def test_exception_message_at_limit_keeps_redaction(self) -> None:
        """上限ちょうどの例外文は検査後に秘匿して保持する。"""
        suffix = " token=synthetic"
        prefix = "x" * (TEXT_LIMIT - len(suffix))
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
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
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
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
    """通常項目・ネスト・例外・診断の文字数を合算し、上限を超えたらログ全体を置換する。"""

    def test_processor_text_at_budget_is_not_exceeded(self) -> None:
        """イベント合計16000文字ちょうどで完了すれば通常出力を保持する。"""
        fields = {key: "x" * (TEXT_LIMIT - 1) for key in "abcd"}
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset(fields))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()), "info", {**fields}
        )
        assert output == {**fields, "log_policy": rules.policy.value}

    def test_processor_text_above_budget_discards_all_original_fields(self) -> None:
        """合計文字数が一文字でも超過すると先行する正常値も固定ログへ置換する。"""
        fields = {key: "x" * (TEXT_LIMIT - 1) for key in "abc"}
        fields["d"] = "x" * TEXT_LIMIT
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset(fields))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()), "info", {**fields}
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "text_total",
        }

    def test_nested_text_at_budget_preserves_log(self) -> None:
        """トップレベル名とネストのキー・値を含め16000文字ならログを保持する。"""
        payload = {key: "x" * (TEXT_LIMIT - 1) for key in "abcd"}
        payload["a"] = "x" * (TEXT_LIMIT - 1 - len("event"))
        assert (
            len("event") + sum(len(k) + len(v) for k, v in payload.items())
            == EVENT_TEXT_LIMIT
        )
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": payload},
        )
        assert output == {"event": payload}

    def test_nested_text_above_budget_replaces_whole_log(self) -> None:
        """ネストを含む合計が16001文字になるとログ全体を置換する。"""
        payload = {key: "x" * (TEXT_LIMIT - 1) for key in "abcd"}
        payload["a"] = "x" * (TEXT_LIMIT - len("event"))
        assert (
            len("event") + sum(len(k) + len(v) for k, v in payload.items())
            == EVENT_TEXT_LIMIT + 1
        )
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": payload},
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "text_total",
        }

    def test_nested_keys_at_text_budget_preserve_log(self) -> None:
        """トップレベル名とネストのキー名で16000文字なら数値の値を保持する。"""
        payload = {letter * TEXT_LIMIT: 1 for letter in "abc"}
        payload["d" * (TEXT_LIMIT - len("event"))] = 1
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": payload},
        )
        assert output == {"event": payload}

    def test_nested_keys_share_event_text_budget(self) -> None:
        """数値の値でもネストのキー名を合算し、16001文字でログ全体を置換する。"""
        payload = {letter * TEXT_LIMIT: 1 for letter in "abc"}
        payload["d" * (TEXT_LIMIT - len("event") + 1)] = 1
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": payload},
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "text_total",
        }

    def test_denied_long_value_does_not_consume_event_text(self) -> None:
        """禁止値が合計上限より長くても、除外後の正常なログを保持する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": {"password": "x" * (EVENT_TEXT_LIMIT + 1), "count": 1}},
        )
        assert output == {"event": {"count": 1}, "_denied_nested_count": 1}

    def test_local_replacement_does_not_refund_preserved_text(self) -> None:
        """長文を局所置換しても保持した文字数は予算に残り、次の項目で超過する。"""
        payload = {key: "x" * (TEXT_LIMIT - 1) for key in "abcd"}
        payload["d"] = "x" * (TEXT_LIMIT - 1 - len("eventlong"))
        payload["long"] = "x" * (TEXT_LIMIT + 1)
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"z"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": payload, "z": 1},
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "text_total",
        }

    def test_exception_text_shares_budget_with_normal_fields(self) -> None:
        """processorが例外用に文字数予算を作り直さない。"""
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset("abcd"))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "error",
            {
                **{key: "x" * (TEXT_LIMIT - 1) for key in "abcd"},
                "exc_info": ValueError("failed"),
            },
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "text_total",
        }

    def test_denied_names_share_event_text_budget(self) -> None:
        """禁止キー名の診断出力もイベントの文字数予算を迂回しない。"""
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset("abcd"))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                **{key: "x" * (TEXT_LIMIT - 1) for key in "abcd"},
                "password": "synthetic",
            },
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "text_total",
        }


class TestWholeLogReplacementByItemBudget:
    """除外項目・ネスト・例外・診断も含めた走査件数が上限を超えたらログ全体を置換する。"""

    def test_denied_mapping_entry_at_item_budget_preserves_log(self) -> None:
        """ネストの禁止項目も一件に数え、合計が上限ちょうどなら残りを出力する。"""
        kept = {f"item_{i}": i for i in range(MAX_ITEMS_PER_LOG_EVENT - 2)}
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": {"password": "synthetic", **kept}},
        )
        assert output == {"event": kept, "_denied_nested_count": 1}

    def test_denied_mapping_entry_above_item_budget_replaces_log(self) -> None:
        """除外した禁止項目を含めて一件超えると、診断も含めログ全体を置換する。"""
        kept = {f"item_{i}": i for i in range(MAX_ITEMS_PER_LOG_EVENT - 1)}
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": {"password": "synthetic", **kept}},
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_mixed_containers_at_item_budget_preserve_log(self) -> None:
        """辞書・配列・要素を各一件に数え、合計が上限ちょうどなら保持する。"""
        fields = {"event": {"items": list(range(MAX_ITEMS_PER_LOG_EVENT - 2))}}
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", fields
        )
        assert output == fields

    def test_mixed_containers_above_item_budget_replace_log(self) -> None:
        """辞書と配列を含む入力が一件でも上限を超えるとログ全体を置換する。"""
        fields = {"event": {"items": list(range(MAX_ITEMS_PER_LOG_EVENT - 1))}}
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", fields
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_mixed_containers_across_fields_at_item_budget_preserve_log(self) -> None:
        """二つの項目にある辞書・配列の合計が上限ちょうどなら両方を保持する。"""
        fields = {
            "event": {"items": [1, 2]},
            "payload": {"items": list(range(MAX_ITEMS_PER_LOG_EVENT - 6))},
        }
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()), "info", fields
        )
        assert output == {**fields, "log_policy": "infrastructure"}

    def test_mixed_containers_across_fields_above_item_budget_replace_log(self) -> None:
        """二つ目の項目で共有予算を一件超えると先行項目も含めて置換する。"""
        fields = {
            "event": {"items": [1, 2]},
            "payload": {"items": list(range(MAX_ITEMS_PER_LOG_EVENT - 5))},
        }
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()), "info", fields
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_top_level_count_at_limit_is_not_counted_twice(self) -> None:
        """選別で数えたトップレベル値を再加算せず上限件数ちょうどまで保持する。"""
        names = {f"field_{i}" for i in range(MAX_ITEMS_PER_LOG_EVENT)}
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset(names))
        fields = {key: 1 for key in sorted(names)}
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()), "info", {**fields}
        )
        assert output == {**fields, "log_policy": rules.policy.value}

    def test_top_level_count_above_limit_replaces_whole_log(self) -> None:
        """許可済みトップレベル項目も上限件数を一件超えるとログ全体を置換する。"""
        fields = {f"field_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT + 1)}
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset(fields))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()), "info", fields
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_container_counts_towards_value_limit(self) -> None:
        """リスト自身と要素の合計が上限件数ちょうどならログを保持する。"""
        items = list(range(MAX_ITEMS_PER_LOG_EVENT - 1))
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": items},
        )
        assert output == {"event": items}

    @pytest.mark.parametrize(
        "payload",
        [
            ["safe"] * MAX_ITEMS_PER_LOG_EVENT,
            {f"item_{i}": "safe" for i in range(MAX_ITEMS_PER_LOG_EVENT)},
        ],
        ids=["list", "mapping"],
    )
    def test_container_above_item_budget_replaces_whole_log(self, payload: Any) -> None:
        """コンテナ自身を含めて上限件数を一件超える入力はログ全体を置換する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": payload},
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_mapping_at_item_budget_preserves_log(self) -> None:
        """辞書自身と項目の合計が上限件数ちょうどならログを保持する。"""
        payload = {f"item_{i}": "safe" for i in range(MAX_ITEMS_PER_LOG_EVENT - 1)}
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": payload},
        )
        assert output == {"event": payload}

    def test_top_level_selection_and_nested_values_share_count_limit(self) -> None:
        """未登録項目の検査も値の走査と同じ件数の予算を消費する。"""
        payload = list(range(6))
        # event・payload自身・payloadの要素と合わせて上限を1件だけ超える数にする。
        unknown_count = MAX_ITEMS_PER_LOG_EVENT + 1 - 2 - len(payload)
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                "event": "failed",
                **{f"unknown_{index}": 0 for index in range(unknown_count)},
                "payload": payload,
            },
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_generated_exception_fields_fit_exact_log_item_limit(self) -> None:
        """入力のexc_infoと生成した例外項目を含めて上限ちょうどなら出力する。"""
        unknown_fields = {f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT - 5)}
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {
                "event": "failed",
                "exc_info": ValueError("invalid data"),
                **unknown_fields,
            },
        )
        assert output == {
            "event": "failed",
            "error_class": "builtins.ValueError",
            "error_message": "invalid data",
            "frames": [],
            "_unregistered_count": len(unknown_fields),
        }

    def test_generated_exception_fields_exceed_remaining_log_item_budget(self) -> None:
        """入力走査の残り件数に例外項目が収まらなければログ全体を置換する。"""
        unknown_fields = {f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT - 4)}
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {
                "event": "failed",
                "exc_info": ValueError("token=synthetic"),
                **unknown_fields,
            },
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_exception_frames_at_shared_item_budget_preserve_log(self) -> None:
        """通常項目と例外frameの合計が上限件数ちょうどなら型・原因文・発生位置を保持する。"""

        def fail():
            raise ValueError("failed")

        with pytest.raises(ValueError) as captured:
            fail()
        exc = captured.value.with_traceback(captured.value.__traceback__.tb_next)
        # 通常3項目・例外3項目・frameの4項目を配列要素と同じ予算で数える。
        payload = [1] * (MAX_ITEMS_PER_LOG_EVENT - 10)
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "payload": payload, "exc_info": exc},
        )
        assert output == {
            "event": "failed",
            "payload": payload,
            "log_policy": "infrastructure",
            "error_class": "builtins.ValueError",
            "error_message": "failed",
            "frames": [
                {
                    "file": __file__,
                    "function": "fail",
                    "line": fail.__code__.co_firstlineno + 1,
                }
            ],
        }

    def test_exception_frames_above_shared_item_budget_replace_log(self) -> None:
        """通常項目と例外frameの合計が上限件数を一件超えるとログ全体を置換する。"""

        def fail():
            raise ValueError("failed")

        with pytest.raises(ValueError) as captured:
            fail()
        exc = captured.value.with_traceback(captured.value.__traceback__.tb_next)
        # 通常3項目・例外3項目・frameの4項目を配列要素と同じ予算で数える。
        payload = [1] * (MAX_ITEMS_PER_LOG_EVENT - 10 + 1)
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "payload": payload, "exc_info": exc},
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_budget_overflow_in_denied_key_diagnostics_replaces_whole_log(self) -> None:
        """禁止キー名の準備で走査上限に達しても入力名を含む途中結果を残さない。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {
                "password": "synthetic",
                **{f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT - 1)},
            },
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_exception_and_denied_key_diagnostics_share_log_item_budget(self) -> None:
        """例外項目の計上後に生成する禁止キー診断も同じ残り予算を使う。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {
                "event": "failed",
                "exc_info": ValueError("invalid data"),
                "password": "synthetic",
                **{f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT - 7)},
            },
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }


class TestBudgetOverflowReason:
    """複数の共有予算を同時に超えうる入力で、どの理由を出力するか。"""

    def test_top_level_count_overflow_precedes_value_text_overflow(
        self,
    ) -> None:
        """トップレベルの選別で件数上限を超えたら、値の文字数超過より先に通知する。"""
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                "payload": ["x" * TEXT_LIMIT] * 4,
                **{f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT)},
            },
        )
        assert prepared_event == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }


class TestSqlDiagnosticBudgets:
    """SQL診断と原因連鎖も通常項目と同じ予算で制限する。"""

    def test_diagnostic_name_at_text_limit_is_preserved(self) -> None:
        """単一文字列の上限ちょうどの診断名は失われない。"""
        exc = PostgresError.new({"C": "23505", "M": "duplicate", "n": "x" * TEXT_LIMIT})
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
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
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
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

    def test_cause_diagnostics_share_item_budget_with_normal_fields(self) -> None:
        """通常配列と原因の診断属性の合計件数でログ全体の上限を判定する。"""
        outer = RuntimeError("wrapped")
        outer.__cause__ = PostgresError.new({"C": "23505", "M": "duplicate"})
        rules = BASE_LOG_RULES.extend(allow=frozenset({"payload"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "error",
            {"payload": [0] * 250, "exc_info": outer},
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_long_diagnostic_attribute_preserves_other_fields(self) -> None:
        """一つの診断名が長すぎても兄弟のSQLSTATEを残す。"""
        exc = IntegrityError(
            "INSERT ...",
            (),
            PostgresError.new(
                {"C": "23505", "M": "duplicate", "n": "x" * (TEXT_LIMIT + 1)}
            ),
        )
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "exc_info": exc},
        )
        assert output["error_details"] == {
            "kind": "postgresql",
            "sqlstate": "23505",
            "constraint_name": "[limit]",
        }
