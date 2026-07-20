"""AnsweringRunner 既存workflowテスト用の固定allow checker。"""

from __future__ import annotations

from uuid import UUID

from app.agent.input_safety.contract import (
    InputSafetyCheckResult,
    InputSafetyPreviousTurn,
    InputSafetyResult,
)


class AllowInputSafetyChecker:
    async def check(
        self,
        *,
        question: str,
        previous_turn: InputSafetyPreviousTurn | None,
        run_id: UUID,
    ) -> InputSafetyCheckResult:
        del question, previous_turn, run_id
        return InputSafetyCheckResult(
            input_safety_result=InputSafetyResult.ALLOW,
            block_reason=None,
        )
