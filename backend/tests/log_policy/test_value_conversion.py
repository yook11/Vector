"""値の操作・型変換と、検査の順序・停止・状態分離の契約。"""

from unittest.mock import patch

import pytest

from app.log_policy.base import BASE_DENY
from app.log_policy.budget import (
    MAX_ITEMS_PER_LOG_EVENT,
    TEXT_LIMIT,
    LogBudgetExceeded,
    LogEventBudget,
)
from app.log_policy.value_preparation import LogValuePreparer

pytestmark = pytest.mark.unit


class TestTypeConversion:
    """対応する型は値と型を保って変換し、非有限・set・派生型・非文字列キーは固定マーカーにする。"""

    def test_null_value_is_preserved(self) -> None:
        """nullは文字列化せず、そのまま出力する。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = None
        preparer.budget.check_and_count_log_items(1)
        assert preparer.prepare_field_value(field_value) is None

    @pytest.mark.parametrize("value", [True, False])
    def test_boolean_value_keeps_its_type(self, value: bool) -> None:
        """真偽値は整数へ変換せず、元の型と値を維持する。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = value
        preparer.budget.check_and_count_log_items(1)
        prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value is value

    def test_finite_float_is_preserved(self) -> None:
        """有限の浮動小数は出力可能な数値として残す。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = 1.25
        preparer.budget.check_and_count_log_items(1)
        assert preparer.prepare_field_value(field_value) == 1.25

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_nonfinite_float_is_replaced(self, value: float) -> None:
        """非有限の浮動小数はJSONへ渡さず固定マーカーにする。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = value
        preparer.budget.check_and_count_log_items(1)
        assert preparer.prepare_field_value(field_value) == "[non-finite]"

    def test_tuple_elements_are_converted_to_a_list(self) -> None:
        """tupleは要素の順序を保ち、各文字列を保護したリストへ変換する。"""
        value = ("token=synthetic", 3)
        preparer = LogValuePreparer(BASE_DENY)
        field_value = value
        preparer.budget.check_and_count_log_items(1)
        assert preparer.prepare_field_value(field_value) == ["token=***", 3]

    def test_mapping_with_mixed_keys_is_replaced(self) -> None:
        """文字列キーが混ざっていても非文字列キーがあれば辞書全体を置換する。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = {200: "synthetic", "count": 1}
        preparer.budget.check_and_count_log_items(1)
        prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == "[non-string-key]"

    def test_nonstring_mapping_does_not_inspect_inner_values(self, monkeypatch) -> None:
        """非文字列キーを含む辞書は、正常なキーの値も含めて中身を検査しない。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = {"message": "token=synthetic", 200: "private"}
        inspected_values = []
        original = preparer.inspect_value

        def inspect(value, depth=0):
            inspected_values.append(value)
            return original(value, depth)

        monkeypatch.setattr(preparer, "inspect_value", inspect)
        assert preparer.prepare_field_value(field_value) == "[non-string-key]"
        assert inspected_values == [field_value]

    def test_mapping_subclass_is_rejected_without_iteration(self) -> None:
        """辞書の派生型は独自の走査処理を呼ばず固定マーカーにする。"""

        class CustomMapping(dict):
            def items(self):
                raise AssertionError("custom iteration must not run")

        value = CustomMapping(password="synthetic")
        preparer = LogValuePreparer(BASE_DENY)
        field_value = value
        preparer.budget.check_and_count_log_items(1)
        assert preparer.prepare_field_value(field_value) == "[unsupported]"

    def test_string_subclass_is_rejected_without_stringification(self) -> None:
        """文字列の派生型は独自の文字列化を呼ばず固定マーカーにする。"""

        class CustomString(str):
            def __str__(self):
                raise AssertionError("custom stringification must not run")

        preparer = LogValuePreparer(BASE_DENY)
        field_value = CustomString("synthetic")
        preparer.budget.check_and_count_log_items(1)
        assert preparer.prepare_field_value(field_value) == "[unsupported]"

    def test_set_is_replaced_as_unsupported_value(self) -> None:
        """setは出力可能な型に含めず、その位置を固定マーカーにする。"""
        preparer = LogValuePreparer(BASE_DENY)
        preparer.budget.check_and_count_log_items(1)
        assert preparer.prepare_field_value({"synthetic"}) == "[unsupported]"


class TestNestedDeny:
    """ネストでも正規化した完全一致で deny を判定し、基本項目と同名の数値は残す。"""

    def test_nested_base_field_name_preserves_numeric_value(self) -> None:
        """基本項目と同じ名前でもネスト内の数値を通常の値として保持する。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = {"event": 3}
        preparer.budget.check_and_count_log_items(1)
        prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == {"event": 3}

    @pytest.mark.parametrize("key", ["apiKey", "API_KEY", "api-key"])
    def test_nested_deny_normalizes_key_aliases(self, key: str) -> None:
        """ネストの直接判定でも表記揺れを正規化してdenyに照合する。"""
        preparer = LogValuePreparer(BASE_DENY)
        field_value = {key: "synthetic", "count": 1}
        preparer.budget.check_and_count_log_items(1)
        prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == {"count": 1}
        assert preparer.diagnostics.as_fields() == {"_denied_nested_count": 1}


class TestInspectionOrder:
    """各項目を計上してから検査し、超過した項目は検査せず後続も取り出さず、サニタイズは成功後に一度だけ行う。"""

    def test_denied_fields_stop_being_inspected_when_budget_is_exhausted(
        self,
        monkeypatch,
    ) -> None:
        """共有予算を超えた禁止項目は正規化せず、後続項目も処理しない。"""
        from app.log_policy import value_preparation

        normalized_keys = []
        original = value_preparation.normalize_key

        def normalize(key):
            normalized_keys.append(key)
            return original(key)

        monkeypatch.setattr(value_preparation, "normalize_key", normalize)
        preparer = LogValuePreparer(
            BASE_DENY, budget=LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 2)
        )
        with pytest.raises(LogBudgetExceeded, match="^value_count$"):
            field_value = {"password": "synthetic", "token": "synthetic", "count": 1}
            preparer.budget.check_and_count_log_items(1)
            preparer.prepare_field_value(field_value)
        assert preparer.diagnostics.as_fields() == {"_denied_nested_count": 1}
        assert preparer.budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT
        assert normalized_keys == ["password"]

    def test_mapping_counts_each_entry_before_value_inspection(
        self, monkeypatch
    ) -> None:
        """辞書の各値を検査するときには、その項目が一件として計上済みである。"""
        preparer = LogValuePreparer(BASE_DENY)
        counts = []
        original = preparer.inspect_value

        def inspect(value, depth=0):
            counts.append(preparer.budget.log_item_count)
            return original(value, depth)

        monkeypatch.setattr(preparer, "inspect_value", inspect)
        assert preparer.inspect_dictionary({"first": 1, "second": 2}, depth=0) == {
            "first": 1,
            "second": 2,
        }
        assert counts == [1, 2]

    def test_sequence_stops_before_inspecting_item_over_limit(
        self, monkeypatch
    ) -> None:
        """上限を超えた配列要素を検査せず、後続要素も取り出さない。"""

        class InputSequence(list):
            def __iter__(self):
                yield 1
                yield 2
                raise AssertionError("must not continue after limit")

        preparer = LogValuePreparer(
            BASE_DENY, budget=LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 1)
        )
        inspected_values = []
        original = preparer.inspect_value

        def inspect(value, depth=0):
            inspected_values.append(value)
            return original(value, depth)

        monkeypatch.setattr(preparer, "inspect_value", inspect)
        with pytest.raises(LogBudgetExceeded, match="^value_count$"):
            preparer.inspect_sequence(InputSequence(), depth=0)
        assert preparer.budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT
        assert inspected_values == [1]

    def test_mapping_stops_before_inspecting_entry_over_limit(
        self, monkeypatch
    ) -> None:
        """上限を超えた項目の値を検査せず、後続項目も取り出さない。"""

        class InputMapping(dict):
            def items(self):
                yield "last_allowed", 1
                yield "over_limit", 2
                raise AssertionError("must not continue after limit")

        preparer = LogValuePreparer(
            BASE_DENY, budget=LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 1)
        )
        inspected_values = []
        original = preparer.inspect_value

        def inspect(value, depth=0):
            inspected_values.append(value)
            return original(value, depth)

        monkeypatch.setattr(preparer, "inspect_value", inspect)
        with pytest.raises(LogBudgetExceeded, match="^value_count$"):
            preparer.inspect_dictionary(InputMapping(), depth=0)
        assert preparer.budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT
        assert inspected_values == [1]

    def test_successful_field_sanitizes_each_text_once(self) -> None:
        """検査段階でサニタイズを実行せず成功後に各文字列を一度だけ処理する。"""
        from app.log_policy.sanitize import sanitize_text

        with patch(
            "app.log_policy.value_preparation.sanitize_text", wraps=sanitize_text
        ) as sanitize:
            preparer = LogValuePreparer(BASE_DENY)
            field_value = {"message": "token=synthetic"}
            preparer.budget.check_and_count_log_items(1)
            prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == {"message": "token=***"}
        assert [call.args[0] for call in sanitize.call_args_list] == [
            "message",
            "token=synthetic",
        ]

    def test_text_above_limit_is_replaced_without_sanitizing(self) -> None:
        """一文字でも超過した文字列にはサニタイズを実行しない。"""
        with patch("app.log_policy.value_preparation.sanitize_text") as sanitize:
            preparer = LogValuePreparer(BASE_DENY)
            field_value = "x" * (TEXT_LIMIT + 1)
            preparer.budget.check_and_count_log_items(1)
            prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == "[limit]"
        sanitize.assert_not_called()

    def test_nested_long_text_is_not_passed_to_sanitization(self) -> None:
        """長すぎる文字列だけ置換し、原文をサニタイズへ渡さず正常な兄弟だけ処理する。"""
        from app.log_policy.sanitize import sanitize_text

        text = "x" * (TEXT_LIMIT + 1)
        payload = {"first": "password=synthetic", "nested": [text]}
        with patch(
            "app.log_policy.value_preparation.sanitize_text", wraps=sanitize_text
        ) as sanitize:
            preparer = LogValuePreparer(BASE_DENY)
            field_value = payload
            preparer.budget.check_and_count_log_items(1)
            prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == {"first": "password=***", "nested": ["[limit]"]}
        assert text not in [call.args[0] for call in sanitize.call_args_list]

    def test_nested_long_key_is_excluded_before_normalization(self) -> None:
        """長すぎるネストのキーは正規化へ渡さず、その項目だけ除外して上限診断に記録する。"""
        with patch("app.log_policy.value_preparation.normalize_key") as normalize:
            preparer = LogValuePreparer(BASE_DENY)
            preparer.budget.check_and_count_log_items(1)
            prepared_value = preparer.prepare_field_value(
                {"x" * (TEXT_LIMIT + 1): "synthetic"}
            )
        assert prepared_value == {}
        assert preparer.diagnostics.as_fields() == {"_policy_limited": True}
        normalize.assert_not_called()


class TestIsolation:
    """呼び出し元の入力を変更せず、単独生成した preparer は診断を共有しない。"""

    def test_mapping_conversion_does_not_mutate_input(self) -> None:
        """辞書の禁止項目を除外しても、呼び出し元のネストした入力は変更しない。"""
        value = {"nested": {"password": "synthetic", "attempt": 2}}
        preparer = LogValuePreparer(BASE_DENY)
        field_value = value
        preparer.budget.check_and_count_log_items(1)
        prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == {"nested": {"attempt": 2}}
        assert value == {"nested": {"password": "synthetic", "attempt": 2}}

    def test_local_limit_does_not_mutate_input(self) -> None:
        """途中まで検査して捨てた辞書も呼び出し元の入力を変更しない。"""
        payload = {"password": "synthetic", "nested": ["x" * (TEXT_LIMIT + 1)]}
        preparer = LogValuePreparer(BASE_DENY)
        field_value = payload
        preparer.budget.check_and_count_log_items(1)
        preparer.prepare_field_value(field_value)
        assert payload == {"password": "synthetic", "nested": ["x" * (TEXT_LIMIT + 1)]}

    def test_standalone_preparers_have_independent_diagnostics(self) -> None:
        """単独生成した値の準備処理は診断状態を共有しない。"""
        first = LogValuePreparer(BASE_DENY)
        second = LogValuePreparer(BASE_DENY)
        field_value = {"password": "synthetic"}
        first.budget.check_and_count_log_items(1)
        first.prepare_field_value(field_value)
        assert second.diagnostics.as_fields() == {}
