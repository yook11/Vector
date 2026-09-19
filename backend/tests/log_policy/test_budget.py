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
    """出力側で使う件数計上の境界と、超過時に部分加算しない契約。"""

    def test_check_and_count_log_items_accepts_exact_shared_limit(self) -> None:
        """複数項目の追加で共有上限ちょうどになった場合は計上できる。"""
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 3)
        budget.check_and_count_log_items(3)
        assert budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT

    def test_check_and_count_log_items_rejects_batch_without_partial_count(
        self,
    ) -> None:
        """まとめて計上する項目が上限を超える場合は途中まで加算しない。"""
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 2)
        with pytest.raises(LogBudgetExceeded, match="^value_count$"):
            budget.check_and_count_log_items(3)
        assert budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT - 2


class TestTextAccounting:
    """出力側へ文字数予算の超過を通知する契約。"""

    def test_text_budget_accepts_exact_shared_limit(self) -> None:
        """合計文字数が上限ちょうどなら計上して継続する。"""
        budget = LogEventBudget(counted_text_chars=EVENT_TEXT_LIMIT - 3)
        budget.check_and_count_text_chars(3)
        assert budget.counted_text_chars == EVENT_TEXT_LIMIT

    def test_text_budget_stops_on_shared_limit_exceeded(self) -> None:
        """合計文字数の超過はログ全体を中断する理由として通知する。"""
        budget = LogEventBudget(counted_text_chars=EVENT_TEXT_LIMIT)
        with pytest.raises(LogBudgetExceeded, match="text_total"):
            budget.check_and_count_text_chars(1)
