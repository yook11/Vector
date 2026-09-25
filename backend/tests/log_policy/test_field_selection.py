"""トップレベルの項目の選別・計上とdeny・allow判定の契約。"""

from unittest.mock import patch

import pytest

from app.log_policy.base import BASE_DENY
from app.log_policy.budget import (
    MAX_ITEMS_PER_LOG_EVENT,
    TEXT_LIMIT,
    LogBudgetExceeded,
    LogEventBudget,
)
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.field_selection import LogFieldSelector

pytestmark = pytest.mark.unit


def test_allow_selection_has_no_implicit_metadata_allow() -> None:
    """基本項目も渡されたallowにない場合は未登録として扱う。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset(), BASE_DENY, diagnostics, budget=LogEventBudget()
    )
    assert selector.select_fields({"event": "completed"}) == {}
    assert diagnostics.as_fields() == {"_unregistered_count": 1}


def test_deny_precedes_allow() -> None:
    """denyとallowの両方に該当する項目は禁止項目として除外する。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({"restricted_sample"}),
        frozenset({"restricted_sample"}),
        diagnostics,
        budget=LogEventBudget(),
    )
    assert selector.select_fields({"RestrictedSample": "value"}) == {}
    assert diagnostics.as_fields() == {"_denied_keys": ["RestrictedSample"]}


def test_similar_field_name_is_not_denied_by_partial_match() -> None:
    """禁止名を一部に含む項目を完全一致のdenyで巻き込まない。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({"completion_tokens"}),
        BASE_DENY,
        diagnostics,
        budget=LogEventBudget(),
    )
    assert selector.select_fields({"completion_tokens": 12}) == {
        "completion_tokens": 12
    }
    assert diagnostics.as_fields() == {}


@pytest.mark.parametrize("field_name", ["apiKey", "API_KEY", "api-key"])
def test_deny_normalizes_field_name_aliases(field_name: str) -> None:
    """項目名の表記揺れを正規化してdenyに照合する。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset(), BASE_DENY, diagnostics, budget=LogEventBudget()
    )
    assert selector.select_fields({field_name: "value"}) == {}
    assert diagnostics.as_fields() == {"_denied_keys": [field_name]}


def test_allow_normalizes_registered_field_names() -> None:
    """allowは正規化して照合し、元の表記と異なっても採用する。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({"source_id"}), BASE_DENY, diagnostics, budget=LogEventBudget()
    )
    assert selector.select_fields({"sourceId": 1}) == {"sourceId": 1}
    assert diagnostics.as_fields() == {}


def test_unregistered_field_name_is_not_recorded() -> None:
    """未登録項目は件数だけを診断に残し、入力名を記録しない。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({"source_id"}), BASE_DENY, diagnostics, budget=LogEventBudget()
    )
    assert selector.select_fields({"synthetic private name": "value"}) == {}
    assert diagnostics.as_fields() == {"_unregistered_count": 1}


def test_nonstring_field_name_is_unregistered() -> None:
    """非文字列の項目名は正規化へ渡す前に除外する。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset(), BASE_DENY, diagnostics, budget=LogEventBudget()
    )
    with patch("app.log_policy.field_selection.normalize_key") as normalize:
        assert selector.select_fields({7: "value"}) == {}
    assert diagnostics.as_fields() == {"_unregistered_count": 1}
    normalize.assert_not_called()


@pytest.mark.parametrize(
    "field_name",
    [
        "stack",
        "stack_info",
        "exception",
        "_record",
        "_from_structlog",
        "_log_policy_rules",
        "exc_info",
    ],
)
def test_internal_field_names_are_excluded_before_deny_and_allow(
    field_name: str,
) -> None:
    """内部項目はallowやdenyに含まれていても出力や診断へ残さない。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({field_name}),
        frozenset({field_name}),
        diagnostics,
        budget=LogEventBudget(),
    )
    assert selector.select_fields({field_name: "value"}) == {}
    assert diagnostics.as_fields() == {}


def test_field_name_at_length_limit_is_preserved() -> None:
    """長さの上限ちょうどの項目名はallowにあれば採用する。"""
    field_name = "a" * TEXT_LIMIT
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({field_name}), BASE_DENY, diagnostics, budget=LogEventBudget()
    )
    assert selector.select_fields({field_name: 1}) == {field_name: 1}
    assert diagnostics.as_fields() == {}


def test_long_field_name_is_not_normalized() -> None:
    """長すぎる項目名は正規化へ渡さず上限診断だけを残す。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset(), BASE_DENY, diagnostics, budget=LogEventBudget()
    )
    with patch("app.log_policy.field_selection.normalize_key") as normalize:
        assert selector.select_fields({"a" * (TEXT_LIMIT + 1): "value"}) == {}
    assert diagnostics.as_fields() == {"_policy_limited": True}
    normalize.assert_not_called()


def test_selected_fields_keep_input_order_and_value_references() -> None:
    """受け付けた項目は値を複製せず、入力の順のまま返す。"""
    first_value = object()
    second_value = object()
    selector = LogFieldSelector(
        frozenset({"first", "second"}),
        BASE_DENY,
        LogProcessingDiagnostics(),
        budget=LogEventBudget(),
    )
    selected_fields = selector.select_fields(
        {"second": second_value, "unknown": object(), "first": first_value}
    )
    assert list(selected_fields) == ["second", "first"]
    assert selected_fields["second"] is second_value
    assert selected_fields["first"] is first_value


def test_every_field_is_counted_and_only_selected_names_add_text() -> None:
    """不採用の項目も件数に数え、文字数は受け付けた項目名の分だけ数える。"""
    budget = LogEventBudget()
    selector = LogFieldSelector(
        frozenset({"source_id"}), BASE_DENY, LogProcessingDiagnostics(), budget=budget
    )
    selector.select_fields(
        {"source_id": 1, "password": "value", "unknown": 2, 7: 3, "_record": 4}
    )
    assert budget.log_item_count == 5
    assert budget.counted_text_chars == len("source_id")


def test_item_overflow_stops_before_reading_next_field() -> None:
    """件数の上限を超えた項目で中断し、それ以降の項目を取り出さない。"""

    class BoundedInput(dict):
        def items(self):
            for i in range(MAX_ITEMS_PER_LOG_EVENT + 1):
                yield f"unknown_{i}", 1
            raise AssertionError("must not read after budget overflow")

    selector = LogFieldSelector(
        frozenset(), BASE_DENY, LogProcessingDiagnostics(), budget=LogEventBudget()
    )
    with pytest.raises(LogBudgetExceeded) as exc_info:
        selector.select_fields(BoundedInput())
    assert exc_info.value.reason == "value_count"
