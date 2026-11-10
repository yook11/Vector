"""渡された根拠だけを引用する回答を生成する工程。"""

from __future__ import annotations

import asyncio

from app.agent.agent import Agent
from app.agent.answering import timing as answer_timing
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
    EvidenceAnswerInput,
)
from app.agent.answering.evidence_answer.failure import EvidenceAnswerError
from app.agent.answering.evidence_answer.validation import (
    finalize_evidence_answer_draft,
)
from app.agent.answering.live_delivery import (
    BestEffortAnswerDeltaReporter,
    close_answer_stream,
)
from app.agent.answering.live_draft import LiveAnswerDraftSession
from app.agent.contract import AnswerDeltaReporter, AnswerGenerationGate
from app.agent.llm_call.caller import (
    AgentTextStream,
    LlmTextStreamer,
    LlmTextStreamerScopeFactory,
)
from app.agent.recording.evidence_answer import (
    EvidenceAnswerFailed,
    EvidenceAnswerRecorder,
    EvidenceAnswerStopped,
    EvidenceAnswerSucceeded,
    logfire_evidence_answer_recorder,
)
from app.agent.retry_prospect import is_resolvable_by_retry
from app.agent.running.state import CanContinue, CannotContinue
from app.ai_providers.errors import AIProviderError

__all__ = ["EvidenceAnswerService"]

_MAX_ATTEMPTS = 2
_EVIDENCE_ANSWER_CLASSIFIED_ERRORS = (
    AIProviderError,
    EvidenceAnswerDraftInvalidError,
)


class EvidenceAnswerService:
    """LLMのstream出力を、渡された根拠だけを引用するdraftへ変換する。"""

    def __init__(
        self,
        *,
        agent: Agent[EvidenceAnswerInput, EvidenceAnswerDraft],
        llm_text_streamer_scope_factory: LlmTextStreamerScopeFactory,
        gate: AnswerGenerationGate,
        delta_reporter: AnswerDeltaReporter | None = None,
        recorder: EvidenceAnswerRecorder = logfire_evidence_answer_recorder,
    ) -> None:
        self._agent = agent
        self._llm_text_streamer_scope_factory = llm_text_streamer_scope_factory
        self._gate = gate
        self._delta = BestEffortAnswerDeltaReporter(delta_reporter)
        self._recorder = recorder

    async def answer(
        self, input: EvidenceAnswerInput
    ) -> EvidenceAnswerDraft | CannotContinue:
        """再試行を含む時間枠内でdraftを完成させ、生成不能は例外で通知する。"""

        match await self._gate.start():
            case CanContinue():
                pass
            case CannotContinue() as stopped:
                return stopped

        timeout = asyncio.timeout(answer_timing.ANSWER_GENERATION_TIMEOUT_SECONDS)
        async with self._recorder.record(agent_name=self._agent.name) as recording:
            attempt_number = 0
            try:
                try:
                    async with timeout:
                        async with self._llm_text_streamer_scope_factory() as streamer:
                            for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                                if attempt_number > 1:
                                    match await self._gate.regenerate():
                                        case CanContinue():
                                            await self._delta.reset(
                                                generation=attempt_number
                                            )
                                        case CannotContinue() as stopped:
                                            recording.report_outcome(
                                                EvidenceAnswerStopped()
                                            )
                                            return stopped
                                try:
                                    draft = await self._generate_strict_draft(
                                        streamer=streamer,
                                        input=input,
                                        attempt_number=attempt_number,
                                    )
                                except _EVIDENCE_ANSWER_CLASSIFIED_ERRORS as exc:
                                    # 接地違反は生成し直せば解消しうる。
                                    retriable = (
                                        isinstance(exc, EvidenceAnswerDraftInvalidError)
                                        or is_resolvable_by_retry(exc)
                                    ) and attempt_number < _MAX_ATTEMPTS
                                    if not retriable:
                                        raise EvidenceAnswerError(cause=exc) from exc
                                    continue
                                break
                            else:
                                raise AssertionError(
                                    "unreachable: attempt budget must settle an outcome"
                                )
                except TimeoutError as cause:
                    if not timeout.expired():
                        raise
                    raise EvidenceAnswerError(cause=cause) from cause
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
        streamer: LlmTextStreamer,
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
                stream = streamer.stream_text(
                    self._agent,
                    input,
                    attempt_number=attempt_number,
                )
                async for fragment in stream:
                    raw_fragments.append(fragment)
                    await live_draft.append(fragment)

                answer = "".join(raw_fragments)
                draft = finalize_evidence_answer_draft(
                    answer, evidence=list(input.evidence)
                )

                await live_draft.commit()
                return draft
        finally:
            await close_answer_stream(stream)
