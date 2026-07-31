"""AnsweringRunner 既存workflowテスト用の固定allow checker。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.agent.input_safety.contract import (
    InputSafetyCheckResult,
    InputSafetyPreviousTurn,
    InputSafetyResult,
)


class _CallTimeline(Protocol):
    """呼び出し順序を記録するtest timelineの最小契約。

    構造的型にして、実装(CallTimeline)への依存を避ける。
    """

    def record(self, event: str) -> None: ...


class AllowInputSafetyChecker:
    def __init__(self, *, timeline: _CallTimeline | None = None) -> None:
        self._timeline = timeline

    async def check(
        self,
        *,
        question: str,
        previous_turn: InputSafetyPreviousTurn | None,
        run_id: UUID,
    ) -> InputSafetyCheckResult:
        del question, previous_turn, run_id
        if self._timeline is not None:
            self._timeline.record("input_safety_checker.check")
        return InputSafetyCheckResult(
            input_safety_result=InputSafetyResult.ALLOW,
            block_reason=None,
        )
