"""Lambdaの制限時間より手前に置く、処理の開始期限を表す。"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol


class LambdaContext(Protocol):
    """AWSのLambdaランタイムがhandlerへ渡すcontextのうち、使う操作だけを表す。"""

    def get_remaining_time_in_millis(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ProcessingStartDeadline:
    """Lambdaの制限時間より手前に置く、ある処理を始めてよい期限。"""

    context: LambdaContext
    before_lambda_limit: timedelta

    def has_passed(self) -> bool:
        remaining = timedelta(milliseconds=self.context.get_remaining_time_in_millis())
        return remaining < self.before_lambda_limit
