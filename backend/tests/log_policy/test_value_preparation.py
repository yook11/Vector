"""値準備の上限・変換・共有予算・検査順序・状態分離を単体で検証する。"""

from unittest.mock import patch

import pytest

from app.log_policy.base import BASE_DENY
from app.log_policy.budget import (
    EVENT_TEXT_LIMIT,
    MAX_ITEMS_PER_LOG_EVENT,
    TEXT_LIMIT,
    LogBudgetExceeded,
    LogEventBudget,
)
from app.log_policy.value_preparation import LogValuePreparer, LogValueRejected

pytestmark = pytest.mark.unit


def _prepare(preparer: LogValuePreparer, field_value: object) -> object:
    """processorと同じく、値を数えてからポリシーをかける。"""
    return preparer.apply_policy(preparer.count_field_value(field_value))


class TestTypeConversion:
    """スカラーは値と型を保ち、tupleはlistにし、辞書と入れ子は形の違反として知らせる。"""

    def test_null_value_is_preserved(self) -> None:
        """nullは文字列化せず、そのまま出力する。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = None
        preparer.budget.check_and_count_log_items(1)
        assert _prepare(preparer, field_value) is None

    @pytest.mark.parametrize("value", [True, False])
    def test_boolean_value_keeps_its_type(self, value: bool) -> None:
        """真偽値は整数へ変換せず、元の型と値を維持する。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = value
        preparer.budget.check_and_count_log_items(1)
        prepared_value = _prepare(preparer, field_value)
        assert prepared_value is value

    def test_finite_float_is_preserved(self) -> None:
        """有限の浮動小数は出力可能な数値として残す。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = 1.25
        preparer.budget.check_and_count_log_items(1)
        assert _prepare(preparer, field_value) == 1.25

    def test_tuple_elements_are_converted_to_a_list(self) -> None:
        """tupleは要素の順序を保ち、各文字列を保護したリストへ変換する。"""
        value = ("token=synthetic", 3)
        preparer = LogValuePreparer(BASE_DENY)
        field_value = value
        preparer.budget.check_and_count_log_items(1)
        assert _prepare(preparer, field_value) == [
            "token=[redacted:credential]",
            3,
        ]

    @pytest.mark.parametrize(
        "value",
        [{"message": "synthetic"}, [["synthetic"]], [{"message": "synthetic"}]],
        ids=["dict", "nested_list", "dict_in_list"],
    )
    def test_nested_shape_is_rejected(self, value: object) -> None:
        """辞書と入れ子の値は、値の形の違反として知らせる。"""
        preparer = LogValuePreparer(BASE_DENY)

        with pytest.raises(LogValueRejected) as exc_info:
            preparer.count_field_value(value)

        assert exc_info.value.reason == "value_shape_invalid"


class TestNestedDeny:
    """ネストでも正規化した完全一致で deny を判定する。"""

    @pytest.mark.parametrize("key", ["apiKey", "API_KEY", "api-key"])
    def test_nested_deny_normalizes_key_aliases(self, key: str) -> None:
        """ログに出す形を整える段でも、ネストのキーの表記揺れを正規化してdenyに照合する。"""
        preparer = LogValuePreparer(BASE_DENY)
        prepared_value = preparer.apply_policy({key: "synthetic", "count": 1})
        assert prepared_value == {"count": 1}
        assert preparer.diagnostics.as_fields() == {"_denied_nested_count": 1}


class TestInspectionOrder:
    """上限を超えた要素は検査せず、後続も取り出さない。"""

    def test_sequence_stops_before_inspecting_item_over_limit(self) -> None:
        """上限を超えた配列要素を検査せず、後続要素も取り出さない。"""

        class InputSequence(list):
            def __iter__(self):
                yield 1
                # 検査すれば形の違反になる要素を置き、件数の確認が先だと確かめる。
                yield {"message": "synthetic"}
                raise AssertionError("must not continue after limit")

        preparer = LogValuePreparer(
            BASE_DENY, budget=LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 1)
        )
        with pytest.raises(LogBudgetExceeded, match="^value_count_exceeded$"):
            preparer.count_field_value(InputSequence())
        assert preparer.budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT


class TestTextLimits:
    """文字列の上限境界と、上限を超えた文字列を加工せずに知らせることを検証する。"""

    def test_text_at_limit_is_preserved_and_redacted_once(self) -> None:
        """単一上限ちょうどの文字列は全文を計上し、一度だけ情報漏洩防止へ渡す。"""
        from app.log_policy.leak_prevention import prevent_leaks

        text = "あ" * TEXT_LIMIT
        preparer = LogValuePreparer(BASE_DENY)

        with patch(
            "app.log_policy.value_preparation.prevent_leaks",
            wraps=prevent_leaks,
        ) as redact:
            prepared_value = _prepare(preparer, text)

        assert prepared_value == text
        assert preparer.budget.counted_text_chars == TEXT_LIMIT
        redact.assert_called_once_with(text)

    def test_text_above_limit_is_rejected_without_leak_prevention(self) -> None:
        """上限をまたぐ秘密を含む文字列は、原文の文字数を加算せず情報漏洩防止も適用せず、上限の超過として知らせる。"""
        budget = LogEventBudget(counted_text_chars=100)
        with patch("app.log_policy.value_preparation.prevent_leaks") as redact:
            preparer = LogValuePreparer(BASE_DENY, budget=budget)
            # 合成値を分割し、秘密検出ツールの規則に一致させない。
            field_value = (
                "x" * (TEXT_LIMIT - 10) + "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
            )
            preparer.budget.check_and_count_log_items(1)
            with pytest.raises(LogValueRejected) as exc_info:
                preparer.count_field_value(field_value)
        assert exc_info.value.reason == "text_length_exceeded"
        assert budget.counted_text_chars == 100
        redact.assert_not_called()

    def test_long_text_in_sequence_is_rejected(self) -> None:
        """listの要素の長すぎる文字列も、上限の超過として知らせる。"""
        values = ["token=first", "x" * (TEXT_LIMIT + 1), "token=first"]
        preparer = LogValuePreparer(BASE_DENY)

        with pytest.raises(LogValueRejected) as exc_info:
            preparer.count_field_value(values)

        assert exc_info.value.reason == "text_length_exceeded"


class TestSharedBudget:
    """値の内部を既存予算へ計上し、超過時は呼び出し元へ通知する。"""

    def test_sequence_at_item_budget_is_prepared(self) -> None:
        """一覧の最後の要素で共有件数上限ちょうどになる場合は全要素を準備する。"""
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 2)
        preparer = LogValuePreparer(BASE_DENY, budget=budget)

        prepared_value = _prepare(preparer, ["token=first", "password=second"])

        assert prepared_value == [
            "token=[redacted:credential]",
            "password=[redacted:credential]",
        ]
        assert budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT

    def test_sequence_item_overflow_is_reported(self) -> None:
        """後続要素で共有件数を超過した場合は、件数の超過として中断する。"""
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 1)
        preparer = LogValuePreparer(BASE_DENY, budget=budget)

        with pytest.raises(LogBudgetExceeded, match="^value_count_exceeded$"):
            preparer.count_field_value(["token=first", "password=second"])

    def test_sequence_at_text_budget_is_prepared(self) -> None:
        """置換前の文字数を合算して共有文字数上限ちょうどなら一覧を準備する。"""
        budget = LogEventBudget(
            counted_text_chars=EVENT_TEXT_LIMIT - len("token=firstpassword=second")
        )
        preparer = LogValuePreparer(BASE_DENY, budget=budget)

        prepared_value = _prepare(preparer, ["token=first", "password=second"])

        assert prepared_value == [
            "token=[redacted:credential]",
            "password=[redacted:credential]",
        ]
        assert budget.counted_text_chars == EVENT_TEXT_LIMIT

    def test_sequence_text_overflow_is_reported(self) -> None:
        """後続要素で共有文字数を超過した場合は、文字数の超過として中断する。"""
        budget = LogEventBudget(
            counted_text_chars=EVENT_TEXT_LIMIT - len("token=first")
        )
        preparer = LogValuePreparer(BASE_DENY, budget=budget)

        with pytest.raises(LogBudgetExceeded, match="^total_text_length_exceeded$"):
            preparer.count_field_value(["token=first", "password=second"])


class TestIsolation:
    """単独生成した preparer は診断を共有しない。"""

    def test_standalone_preparers_have_independent_diagnostics(self) -> None:
        """単独生成した値の準備処理は診断状態を共有しない。"""
        first = LogValuePreparer(BASE_DENY)
        second = LogValuePreparer(BASE_DENY)
        first.apply_policy({"password": "synthetic"})
        assert second.diagnostics.as_fields() == {}
