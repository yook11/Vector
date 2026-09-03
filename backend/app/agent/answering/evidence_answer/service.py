"""渡された根拠だけを引用する回答を生成する工程。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
    EvidenceAnswerInput,
)
from app.agent.answering.evidence_answer.failure import EvidenceAnswerError
from app.agent.answering.evidence_answer.validation import (
    finalize_evidence_answer_draft,
)
from app.agent.answering.failure import (
    RequestRetryDisposition,
    classify_answer_synthesis_failure,
)
from app.agent.answering.live_delivery import (
    BestEffortAnswerDeltaReporter,
    close_answer_stream,
    ensure_answer_generation_continues,
)
from app.agent.answering.live_draft import LiveAnswerDraftSession
from app.agent.contract import AnswerDeltaReporter
from app.agent.recording.evidence_answer import (
    EvidenceAnswerFailed,
    EvidenceAnswerRecorder,
    EvidenceAnswerSucceeded,
    logfire_evidence_answer_recorder,
)
from app.agent.runs.execution import RunExecutionContinuation
from app.agent.runtime.contract import (
    AgentTextStream,
    StreamingAgentRuntime,
    StreamingAgentRuntimeScopeFactory,
)
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderOutputTruncatedError,
)

__all__ = ["EvidenceAnswerService"]

_MAX_ATTEMPTS = 2
_ANSWER_TIMEOUT_SECONDS = 15
# ValidationError(pydantic)は、plain text化後のfinalize_evidence_answer_draft()が
# 空白判定を先に行うため通常経路では到達しない。classify_answer_synthesis_failure()側の
# 分類も維持されており、EvidenceAnswerDraftの構築自体が将来pydantic validationで
# 失敗しうる防御的なゲートとして残すため、caught setからは外さない。
_EVIDENCE_ANSWER_CLASSIFIED_ERRORS = (
    AIProviderError,
    EvidenceAnswerDraftInvalidError,
    ValidationError,
)


class EvidenceAnswerService:
    """LLMのstream出力を、渡された根拠だけを引用するdraftへ変換する。"""

    def __init__(
        self,
        *,
        agent: Agent[EvidenceAnswerInput, EvidenceAnswerDraft],
        runtime_scope_factory: StreamingAgentRuntimeScopeFactory,
        delta_reporter: AnswerDeltaReporter | None = None,
        continuation: RunExecutionContinuation | None = None,
        recorder: EvidenceAnswerRecorder = logfire_evidence_answer_recorder,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory
        self._delta = BestEffortAnswerDeltaReporter(delta_reporter)
        self._continuation = continuation
        self._recorder = recorder

    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerDraft:
        """再試行を含む時間枠内でdraftを完成させ、生成不能は例外で通知する。"""

        async with self._recorder.record(agent_name=self._agent.name) as recording:
            attempt_number = 0
            timeout = asyncio.timeout(_ANSWER_TIMEOUT_SECONDS)
            try:
                try:
                    async with timeout:
                        async with self._runtime_scope_factory() as runtime:
                            for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                                try:
                                    draft = await self._generate_strict_draft(
                                        runtime=runtime,
                                        input=input,
                                        attempt_number=attempt_number,
                                    )
                                except _EVIDENCE_ANSWER_CLASSIFIED_ERRORS as exc:
                                    failure = classify_answer_synthesis_failure(exc)
                                    retriable = (
                                        failure.request_retry_disposition
                                        is RequestRetryDisposition.RETRY_IN_REQUEST
                                        and attempt_number < _MAX_ATTEMPTS
                                    )
                                    if not retriable:
                                        raise EvidenceAnswerError(
                                            code=failure.code
                                        ) from exc
                                    await self._start_revision(
                                        generation=attempt_number + 1
                                    )
                                    if isinstance(exc, AIProviderOutputTruncatedError):
                                        input = replace(
                                            input, previous_output_truncated=True
                                        )
                                    continue
                                break
                            else:
                                raise AssertionError(
                                    "unreachable: attempt budget must settle an outcome"
                                )
                except TimeoutError as cause:
                    if not timeout.expired():
                        raise
                    raise EvidenceAnswerError(code="evidence_answer_timeout") from cause
            except EvidenceAnswerError as error:
                recording.report_outcome(
                    EvidenceAnswerFailed(
                        failure_code=error.code,
                        attempt_count=attempt_number,
                    )
                )
                raise
            recording.report_outcome(
                EvidenceAnswerSucceeded(attempt_count=attempt_number)
            )
            return draft

    async def _generate_strict_draft(
        self,
        *,
        runtime: StreamingAgentRuntime,
        input: EvidenceAnswerInput,
        attempt_number: int,
    ) -> EvidenceAnswerDraft:
        stream: AgentTextStream | None = None
        raw_fragments: list[str] = []
        try:
            async with LiveAnswerDraftSession(
                generation=attempt_number,
                delta_reporter=self._delta,
            ) as live_draft:
                await ensure_answer_generation_continues(self._continuation)

                stream = runtime.stream_text(
                    self._agent,
                    input,
                    attempt_number=attempt_number,
                )
                async for fragment in stream:
                    await ensure_answer_generation_continues(self._continuation)
                    raw_fragments.append(fragment)
                    await live_draft.append(fragment)

                await ensure_answer_generation_continues(self._continuation)
                answer = "".join(raw_fragments)
                draft = finalize_evidence_answer_draft(
                    answer, evidence=list(input.evidence)
                )

                await live_draft.commit()
                return draft
        finally:
            await close_answer_stream(stream)

    async def _start_revision(self, *, generation: int) -> None:
        await ensure_answer_generation_continues(self._continuation)
        await self._delta.reset(generation=generation)
