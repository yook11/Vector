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
from app.log_policy.value_preparation import DEPTH_LIMIT, LogValuePreparer

pytestmark = pytest.mark.unit


class TestPreparationDepth:
    """深さ上限ちょうどの値は保持し、その一段先は検査せず置き換える。"""

    def test_value_at_depth_limit_is_preserved(self) -> None:
        """深さ上限ちょうどにある値は保持する。"""
        value = "diagnostic"
        for _ in range(DEPTH_LIMIT):
            value = [value]
        preparer = LogValuePreparer(BASE_DENY)

        result = preparer.prepare_field_value(value)

        assert result == value

    def test_value_beyond_depth_limit_is_not_inspected(self, monkeypatch) -> None:
        """上限を一段超えた値は、型の検査前にlimitへ置き換える。"""
        from app.log_policy import value_preparation

        omitted = object()
        value = omitted
        expected = "[limit]"
        for _ in range(DEPTH_LIMIT + 1):
            value = [value]
            expected = [expected]
        original = value_preparation.is_supported_value

        def inspect_type(candidate):
            if candidate is omitted:
                pytest.fail("must not inspect a value beyond the depth limit")
            return original(candidate)

        monkeypatch.setattr(value_preparation, "is_supported_value", inspect_type)
        preparer = LogValuePreparer(BASE_DENY)

        result = preparer.prepare_field_value(value)

        assert result == expected


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
        assert preparer.prepare_field_value(field_value) == [
            "token=[redacted:credential]",
            3,
        ]

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

        def inspect(value, depth=0, **kwargs):
            inspected_values.append(value)
            return original(value, depth, **kwargs)

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
    """各項目を計上してから検査し、超過した項目は検査せず後続も取り出さず、情報漏洩防止は成功後に一度だけ行う。"""

    def test_masked_field_skips_value_inspection_and_text_preparation(self) -> None:
        """マスク対象の値は型・内部構造を検査せず、置換後のマーカーにサニタイズも情報漏洩防止も適用しない。"""
        preparer = LogValuePreparer(
            BASE_DENY,
            mask=frozenset({"private_text"}),
            sanitize=frozenset({"private_text"}),
        )

        with (
            patch(
                "app.log_policy.value_preparation.is_supported_value",
                side_effect=AssertionError("must not inspect a masked value"),
            ),
            patch(
                "app.log_policy.value_preparation.sanitize_field_value",
                side_effect=AssertionError("must not sanitize a masked field"),
            ),
            patch(
                "app.log_policy.value_preparation.prevent_credential_leaks",
                side_effect=AssertionError("must not redact a mask marker"),
            ),
        ):
            output = preparer.prepare_field_value(
                {"message": "synthetic"}, field_name="private_text"
            )

        assert output == "***"

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

        def inspect(value, depth=0, **kwargs):
            counts.append(preparer.budget.log_item_count)
            return original(value, depth, **kwargs)

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

        def inspect(value, depth=0, **kwargs):
            inspected_values.append(value)
            return original(value, depth, **kwargs)

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

        def inspect(value, depth=0, **kwargs):
            inspected_values.append(value)
            return original(value, depth, **kwargs)

        monkeypatch.setattr(preparer, "inspect_value", inspect)
        with pytest.raises(LogBudgetExceeded, match="^value_count$"):
            preparer.inspect_dictionary(InputMapping(), depth=0)
        assert preparer.budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT
        assert inspected_values == [1]

    def test_successful_field_redacts_each_text_once(self) -> None:
        """検査段階で情報漏洩防止を実行せず、成功後に辞書のキーと値の各文字列を一度だけ処理する。"""
        from app.log_policy.leak_prevention import prevent_credential_leaks

        with patch(
            "app.log_policy.value_preparation.prevent_credential_leaks",
            wraps=prevent_credential_leaks,
        ) as redact:
            preparer = LogValuePreparer(BASE_DENY)
            field_value = {"password=synthetic": "token=synthetic"}
            preparer.budget.check_and_count_log_items(1)
            prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == {
            "password=[redacted:credential]": "token=[redacted:credential]"
        }
        assert [call.args[0] for call in redact.call_args_list] == [
            "password=synthetic",
            "token=synthetic",
        ]


class TestTextLimits:
    """文字列の上限境界と、超過した値・キーだけを処理対象から外すことを検証する。"""

    def test_text_at_limit_is_preserved_and_redacted_once(self) -> None:
        """単一上限ちょうどの文字列は全文を計上し、一度だけ情報漏洩防止へ渡す。"""
        from app.log_policy.leak_prevention import prevent_credential_leaks

        text = "あ" * TEXT_LIMIT
        preparer = LogValuePreparer(BASE_DENY)

        with patch(
            "app.log_policy.value_preparation.prevent_credential_leaks",
            wraps=prevent_credential_leaks,
        ) as redact:
            prepared_value = preparer.prepare_field_value(text)

        assert prepared_value == text
        assert preparer.budget.counted_text_chars == TEXT_LIMIT
        redact.assert_called_once_with(text)

    def test_text_above_limit_is_replaced_without_leak_prevention(self) -> None:
        """上限をまたぐ秘密を含む文字列は、原文の文字数を加算せず情報漏洩防止も適用せず、断片を残さず置換する。"""
        budget = LogEventBudget(counted_text_chars=100)
        with patch(
            "app.log_policy.value_preparation.prevent_credential_leaks"
        ) as redact:
            preparer = LogValuePreparer(BASE_DENY, budget=budget)
            # 合成値を分割し、秘密検出ツールの規則に一致させない。
            field_value = (
                "x" * (TEXT_LIMIT - 10) + "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
            )
            preparer.budget.check_and_count_log_items(1)
            prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == "[limit]"
        assert budget.counted_text_chars == 100
        redact.assert_not_called()

    def test_sequence_replaces_long_text_preserving_order_and_duplicates(self) -> None:
        """長い要素だけを置換し、保護した前後の文字列の順序と重複を維持する。"""
        values = ["token=first", "x" * (TEXT_LIMIT + 1), "token=first"]
        preparer = LogValuePreparer(BASE_DENY)

        prepared_value = preparer.prepare_field_value(values)

        assert prepared_value == [
            "token=[redacted:credential]",
            "[limit]",
            "token=[redacted:credential]",
        ]

    def test_nested_long_text_is_not_passed_to_leak_prevention(self) -> None:
        """長すぎる文字列だけ置換し、原文を情報漏洩防止へ渡さず正常な兄弟だけ処理する。"""
        from app.log_policy.leak_prevention import prevent_credential_leaks

        text = "x" * (TEXT_LIMIT + 1)
        payload = {"first": "password=synthetic", "nested": [text]}
        with patch(
            "app.log_policy.value_preparation.prevent_credential_leaks",
            wraps=prevent_credential_leaks,
        ) as redact:
            preparer = LogValuePreparer(BASE_DENY)
            field_value = payload
            preparer.budget.check_and_count_log_items(1)
            prepared_value = preparer.prepare_field_value(field_value)
        assert prepared_value == {
            "first": "password=[redacted:credential]",
            "nested": ["[limit]"],
        }
        assert text not in [call.args[0] for call in redact.call_args_list]

    def test_nested_long_key_is_excluded_before_normalization(
        self, monkeypatch
    ) -> None:
        """長すぎるネストのキーは正規化へ渡さず、その項目だけ除外して上限診断に記録し、兄弟は残す。"""
        from app.log_policy import value_preparation

        normalized_keys = []
        original = value_preparation.normalize_key

        def normalize(key):
            normalized_keys.append(key)
            return original(key)

        monkeypatch.setattr(value_preparation, "normalize_key", normalize)
        long_key = "x" * (TEXT_LIMIT + 1)
        preparer = LogValuePreparer(BASE_DENY)
        preparer.budget.check_and_count_log_items(1)
        prepared_value = preparer.prepare_field_value(
            {long_key: "synthetic", "count": 1}
        )
        assert prepared_value == {"count": 1}
        assert preparer.diagnostics.as_fields() == {"_policy_limited": True}
        assert long_key not in normalized_keys


class TestSharedBudget:
    """値の内部を既存予算へ計上し、超過時は文字列を加工せず呼び出し元へ通知する。"""

    def test_sequence_at_item_budget_is_prepared(self) -> None:
        """一覧の最後の要素で共有件数上限ちょうどになる場合は全要素を準備する。"""
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 2)
        preparer = LogValuePreparer(BASE_DENY, budget=budget)

        prepared_value = preparer.prepare_field_value(
            ["token=first", "password=second"]
        )

        assert prepared_value == [
            "token=[redacted:credential]",
            "password=[redacted:credential]",
        ]
        assert budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT

    def test_sequence_item_overflow_stops_before_redacting_any_text(self) -> None:
        """後続要素で共有件数を超過した場合は先行文字列も加工せず中断する。"""
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 1)
        preparer = LogValuePreparer(BASE_DENY, budget=budget)

        with patch(
            "app.log_policy.value_preparation.prevent_credential_leaks"
        ) as redact:
            with pytest.raises(LogBudgetExceeded, match="^value_count$"):
                preparer.prepare_field_value(["token=first", "password=second"])

        redact.assert_not_called()

    def test_sequence_at_text_budget_is_prepared(self) -> None:
        """置換前の文字数を合算して共有文字数上限ちょうどなら一覧を準備する。"""
        budget = LogEventBudget(
            counted_text_chars=EVENT_TEXT_LIMIT - len("token=firstpassword=second")
        )
        preparer = LogValuePreparer(BASE_DENY, budget=budget)

        prepared_value = preparer.prepare_field_value(
            ["token=first", "password=second"]
        )

        assert prepared_value == [
            "token=[redacted:credential]",
            "password=[redacted:credential]",
        ]
        assert budget.counted_text_chars == EVENT_TEXT_LIMIT

    def test_sequence_text_overflow_stops_before_redacting_any_text(self) -> None:
        """後続要素で共有文字数を超過した場合は先行文字列も加工せず中断する。"""
        budget = LogEventBudget(
            counted_text_chars=EVENT_TEXT_LIMIT - len("token=first")
        )
        preparer = LogValuePreparer(BASE_DENY, budget=budget)

        with patch(
            "app.log_policy.value_preparation.prevent_credential_leaks"
        ) as redact:
            with pytest.raises(LogBudgetExceeded, match="^text_total$"):
                preparer.prepare_field_value(["token=first", "password=second"])

        redact.assert_not_called()


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
