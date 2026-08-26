"""Thread-scoped question context preparation with a safe fallback."""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

import structlog
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.phase_span import agent_phase
from app.agent.question_context.contract import (
    AnswerBrief,
    AnswerBriefDraft,
    QuestionContextGenerationInput,
    answer_brief_from_draft,
)
from app.agent.question_context.metrics import QuestionContextOutcome
from app.agent.recording.question_context import (
    QuestionContextRecorder,
    logfire_question_context_recorder,
)
from app.agent.runtime.contract import (
    AgentResponseInvalidError,
    AgentRuntimeScopeFactory,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import AIProviderError

HISTORY_MESSAGE_LIMIT = 6
HISTORY_MESSAGE_CHAR_CAP = 2000
MISSING_ASPECT_CHAR_CAP = 300
MISSING_ASPECT_LIMIT = 8

logger = structlog.get_logger(__name__)
_RUNTIME_FAILURES = (
    AIProviderError,
    AgentResponseInvalidError,
)
_GENERATOR_UNAVAILABLE = "generator_unavailable"
_CONTEXT_FINALIZE_INVALID = "context_finalize_invalid"
_PROVIDER_ERROR = "provider_error"


class QuestionContextService:
    """Prepare question context while preserving existing behavior on failure."""

    def __init__(
        self,
        *,
        agent: Agent[QuestionContextGenerationInput, AnswerBriefDraft],
        runtime_scope_factory: AgentRuntimeScopeFactory | None,
        recorder: QuestionContextRecorder = logfire_question_context_recorder,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory
        self._recorder = recorder

    async def prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> AnswerBrief:
        outcome: QuestionContextOutcome | None = None
        failure_code: str | None = None
        stopped = False
        call = await self._recorder.start()
        try:
            if self._runtime_scope_factory is None:
                outcome = "failed"
                failure_code = _GENERATOR_UNAVAILABLE
                return _fallback_result(
                    question=question,
                    run_id=run_id,
                    failure_code=_GENERATOR_UNAVAILABLE,
                )

            with agent_phase(phase="context_resolution", agent_name=self._agent.name):
                try:
                    async with self._runtime_scope_factory() as runtime:
                        draft = await runtime.call(
                            self._agent,
                            QuestionContextGenerationInput(
                                question=question,
                                history=tuple(_history_for_prompt(history)),
                                as_of=as_of,
                            ),
                            attempt_number=1,
                        )
                except _RUNTIME_FAILURES as exc:
                    outcome = "failed"
                    failure_code = _failure_code(exc)
                    return _fallback_result(
                        question=question,
                        run_id=run_id,
                        failure_code=failure_code,
                    )

                try:
                    answer_brief = answer_brief_from_draft(draft)
                    if not history:
                        answer_brief = AnswerBrief(
                            standalone_question=question,
                            answer_requirements=answer_brief.answer_requirements,
                            relevant_prior_coverage="",
                            active_goal=answer_brief.active_goal,
                        )
                except ValidationError:
                    outcome = "failed"
                    failure_code = _CONTEXT_FINALIZE_INVALID
                    return _fallback_result(
                        question=question,
                        run_id=run_id,
                        failure_code=_CONTEXT_FINALIZE_INVALID,
                    )
                outcome = "prepared"
                return answer_brief
        except (asyncio.CancelledError, GeneratorExit):
            stopped = True
            outcome = None
            failure_code = None
            raise
        finally:
            await self._recorder.end(
                call,
                outcome=outcome,
                prompt_version=self._agent.prompt.version,
                ai_model=self._agent.model.name,
                failure_code=failure_code,
                stopped=stopped,
            )


def _fallback_result(
    *,
    question: str,
    run_id: UUID,
    failure_code: str,
) -> AnswerBrief:
    logger.warning(
        "question_context_preparation_failed",
        run_id=str(run_id),
        failure_type=failure_code,
    )
    return answer_brief_from_draft(AnswerBriefDraft(standalone_question=question))


def _failure_code(error: Exception) -> str:
    if isinstance(error, AgentResponseInvalidError):
        return error.defect.value
    code = getattr(error, "CODE", None)
    if isinstance(code, str) and code:
        return code
    return _PROVIDER_ERROR


def _history_for_prompt(
    history: list[ThreadMessageSnapshot],
) -> list[ThreadMessageSnapshot]:
    seen_missing_aspects: set[str] = set()
    prompt_history: list[ThreadMessageSnapshot] = []
    for message in history:
        missing_aspects: list[str] = []
        if message.role == "assistant":
            for missing_aspect in message.missing_aspects:
                normalized = _normalize_missing_aspect(missing_aspect)
                if (
                    normalized
                    and normalized not in seen_missing_aspects
                    and len(seen_missing_aspects) < MISSING_ASPECT_LIMIT
                ):
                    seen_missing_aspects.add(normalized)
                    missing_aspects.append(normalized)
        prompt_history.append(
            ThreadMessageSnapshot(
                role=message.role,
                content=message.content[:HISTORY_MESSAGE_CHAR_CAP],
                missing_aspects=tuple(missing_aspects),
            )
        )
    return prompt_history


def _normalize_missing_aspect(value: str) -> str:
    return value.strip()[:MISSING_ASPECT_CHAR_CAP].strip()
