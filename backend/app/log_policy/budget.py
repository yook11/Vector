"""ログ値と診断が共有するログ一件の件数・文字数の上限を管理する。"""

from dataclasses import dataclass
from typing import Literal

TEXT_LIMIT = 4000
EVENT_TEXT_LIMIT = 16000
MAX_ITEMS_PER_LOG_EVENT = 256

BudgetLimitReason = Literal["value_count", "text_total"]


class LogBudgetExceeded(Exception):
    """共有予算の超過を通知し、ログ一件の処理を中止する。"""

    def __init__(self, reason: BudgetLimitReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class LogEventBudget:
    """通常項目・例外項目・診断で使う予算を一か所に保持する。"""

    log_item_count: int = 0
    counted_text_chars: int = 0

    def check_and_count_log_items(self, additional_count: int) -> None:
        """追加項目が上限内に収まることを確認して計上し、超過時は中断する。"""
        if self.log_item_count + additional_count > MAX_ITEMS_PER_LOG_EVENT:
            raise LogBudgetExceeded("value_count")
        self.log_item_count += additional_count

    def check_and_count_text_chars(self, additional_chars: int) -> None:
        """保護対象の文字数を合算し、ログ一件の上限を超えたら中断する。"""
        self.counted_text_chars += additional_chars
        if self.counted_text_chars > EVENT_TEXT_LIMIT:
            raise LogBudgetExceeded("text_total")
