"""Lambdaの制限時間より手前に置く、処理を始めてよい制限時間を表す。"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol


class LambdaContext(Protocol):
    """AWSのLambdaランタイムがhandlerへ渡すcontextのうち、使う操作だけを表す。"""

    def get_remaining_time_in_millis(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ProcessingTimeLimit:
    """Lambdaの制限時間より手前に置く、ある処理を始めてよい制限時間。"""

    context: LambdaContext
    before_lambda_limit: timedelta

    def is_exceeded(self) -> bool:
        remaining = timedelta(milliseconds=self.context.get_remaining_time_in_millis())
        return remaining < self.before_lambda_limit
