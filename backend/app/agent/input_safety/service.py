"""Single-attempt input safety check service."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

import structlog

from app.agent.agent import Agent
from app.agent.input_safety.contract import (
    InputSafetyAgentInput,
    InputSafetyAgentOutput,
    InputSafetyBlockReason,
    InputSafetyCheckResult,
    InputSafetyPreviousTurn,
    InputSafetyResult,
)
from app.agent.input_safety.metrics import record_input_safety_outcome
from app.agent.runtime.contract import (
    AgentResponseInvalidError,
    AgentRuntimeScopeFactory,
)
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)

logger = structlog.get_logger(__name__)


class InputSafetyService:
    def __init__(
        self,
        *,
        agent: Agent[InputSafetyAgentInput, InputSafetyAgentOutput],
        runtime_scope_factory: AgentRuntimeScopeFactory,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory

    async def check(
        self,
        *,
        question: str,
        previous_turn: InputSafetyPreviousTurn | None,
        run_id: UUID,
    ) -> InputSafetyCheckResult:
        try:
            async with self._runtime_scope_factory() as runtime:
                output = await runtime.invoke(
                    self._agent,
                    InputSafetyAgentInput(
                        question=question,
                        previous_turn=previous_turn,
                    ),
                    attempt_number=1,
                )
        except Exception as error:
            provider_block = _provider_safety_block(error)
            if provider_block is None:
                _record_failure(
                    error=error,
                    run_id=run_id,
                    agent=self._agent,
                )
                raise
            result = provider_block
        else:
            result = _check_result_from_agent_output(output)

        record_input_safety_outcome(
            result=("block" if result.is_blocked else "allow"),
            block_reason=result.block_reason,
        )
        if result.is_blocked:
            assert result.block_reason is not None  # noqa: S101
            logger.info(
                "agent_input_safety_blocked",
                run_id=str(run_id),
                block_reason=result.block_reason.value,
                ai_model=self._agent.model.name,
                prompt_version=self._agent.prompt.version,
                input_length=len(question),
            )
        return result


def _check_result_from_agent_output(
    output: InputSafetyAgentOutput,
) -> InputSafetyCheckResult:
    return InputSafetyCheckResult(
        input_safety_result=output.input_safety_result,
        block_reason=output.block_reason,
    )


def _provider_safety_block(error: Exception) -> InputSafetyCheckResult | None:
    if (
        isinstance(
            error,
            AIProviderInputRejectedError | AIProviderOutputBlockedError,
        )
        and error.is_safety_rejection
    ):
        return _provider_safety_block_result()
    return None


def _provider_safety_block_result() -> InputSafetyCheckResult:
    return InputSafetyCheckResult(
        input_safety_result=InputSafetyResult.BLOCK,
        block_reason=InputSafetyBlockReason.PROVIDER_SAFETY_FILTER,
    )


def _record_failure(
    *,
    error: Exception,
    run_id: UUID,
    agent: Agent[InputSafetyAgentInput, InputSafetyAgentOutput],
) -> None:
    record_input_safety_outcome(result="failed", block_reason=None)
    fields: dict[str, str] = {
        "run_id": str(run_id),
        "failure_code": _failure_code(error),
        "ai_model": agent.model.name,
        "prompt_version": agent.prompt.version,
    }
    reason = getattr(error, "reason", None)
    if isinstance(reason, StrEnum):
        fields["failure_reason"] = reason.value
    logger.warning("agent_input_safety_failed", **fields)


def _failure_code(error: Exception) -> str:
    if isinstance(error, AgentResponseInvalidError):
        return error.defect.value
    if isinstance(error, AIProviderError):
        return error.CODE
    return "unexpected_error"
