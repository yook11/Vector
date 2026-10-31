"""ログ一件の共有予算の境界を検証する。"""

import pytest

from app.log_policy.budget import (
    EVENT_TEXT_LIMIT,
    MAX_ITEMS_PER_LOG_EVENT,
    LogBudgetExceeded,
    LogEventBudget,
)

pytestmark = pytest.mark.unit


class TestItemAccounting:
    """件数の上限ちょうどは計上し、超えたら理由付きで知らせる。"""

    def test_check_and_count_log_items_accepts_exact_shared_limit(self) -> None:
        """1件足して共有上限ちょうどになる場合は計上できる。"""
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 1)
        budget.check_and_count_log_items(1)
        assert budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT

    def test_check_and_count_log_items_rejects_over_limit(self) -> None:
        """上限ちょうどからさらに1件足すと value_count_exceeded で知らせる。"""
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT)
        with pytest.raises(LogBudgetExceeded) as exc_info:
            budget.check_and_count_log_items(1)
        assert exc_info.value.reason == "value_count_exceeded"


class TestTextAccounting:
    """文字数の上限ちょうどは計上し、超えたら理由付きで知らせる。"""

    def test_text_budget_accepts_exact_shared_limit(self) -> None:
        """足して合計が共有上限ちょうどになる場合は計上できる。"""
        budget = LogEventBudget(counted_text_chars=EVENT_TEXT_LIMIT - 3)
        budget.check_and_count_text_chars(3)
        assert budget.counted_text_chars == EVENT_TEXT_LIMIT

    def test_text_budget_stops_on_shared_limit_exceeded(self) -> None:
        """上限ちょうどからさらに1文字足すと total_text_length_exceeded で知らせる。"""
        budget = LogEventBudget(counted_text_chars=EVENT_TEXT_LIMIT)
        with pytest.raises(LogBudgetExceeded) as exc_info:
            budget.check_and_count_text_chars(1)
        assert exc_info.value.reason == "total_text_length_exceeded"
