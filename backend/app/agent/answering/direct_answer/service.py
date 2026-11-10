"""Validated direct answer service."""

from __future__ import annotations

import asyncio

from app.agent.agent import Agent
from app.agent.answering import timing as answer_timing
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
    DirectAnswerInvalidError,
)
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.answering.live_delivery import (
    BestEffortAnswerDeltaReporter,
    close_answer_stream,
)
from app.agent.answering.live_draft import LiveAnswerDraftSession
from app.agent.citation_markers import strip_citation_markers
from app.agent.contract import AnswerDeltaReporter, AnswerGenerationGate
from app.agent.llm_call.caller import (
    AgentTextStream,
    LlmTextStreamer,
    LlmTextStreamerScopeFactory,
)
from app.agent.recording.direct_answer import (
    DirectAnswerFailed,
    DirectAnswerRecorder,
    DirectAnswerStopped,
    DirectAnswerSucceeded,
    logfire_direct_answer_recorder,
)
from app.agent.retry_prospect import is_resolvable_by_retry
from app.agent.running.state import CanContinue, CannotContinue
from app.ai_providers.errors import AIProviderError

__all__ = ["DirectAnswerService"]

_DIRECT_ANSWER_SOURCE_ERRORS = (AIProviderError, DirectAnswerInvalidError)
_MAX_ATTEMPTS = 2


class DirectAnswerService:
    """Create validated direct answer drafts.

    Propagates classified DirectAnswerError and returns why the run stopped.
    """

    def __init__(
        self,
        *,
        agent: Agent[DirectAnswerInput, DirectAnswerDraft],
        llm_text_streamer_scope_factory: LlmTextStreamerScopeFactory,
        gate: AnswerGenerationGate,
        delta_reporter: AnswerDeltaReporter | None = None,
        recorder: DirectAnswerRecorder = logfire_direct_answer_recorder,
    ) -> None:
        self._agent = agent
        self._llm_text_streamer_scope_factory = llm_text_streamer_scope_factory
        self._gate = gate
        self._delta = BestEffortAnswerDeltaReporter(delta_reporter)
        self._recorder = recorder

    async def answer(
        self, input: DirectAnswerInput
    ) -> DirectAnswerDraft | CannotContinue:
        """完成した回答を返す。空の回答と、呼び直せば解消しうる失敗だけ再試行する。"""

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
                                            pass
                                        case CannotContinue() as stopped:
                                            recording.report_outcome(
                                                DirectAnswerStopped()
                                            )
                                            return stopped
                                try:
                                    draft = await self._generate_draft(
                                        streamer=streamer,
                                        input=input,
                                        attempt_number=attempt_number,
                                    )
                                except _DIRECT_ANSWER_SOURCE_ERRORS as cause:
                                    # 空の回答は生成し直せば解消しうる。
                                    retriable = (
                                        isinstance(cause, DirectAnswerInvalidError)
                                        or is_resolvable_by_retry(cause)
                                    ) and attempt_number < _MAX_ATTEMPTS
                                    if retriable:
                                        continue
                                    raise DirectAnswerError(cause=cause) from cause
                                break
                            else:
                                raise AssertionError(
                                    "unreachable: answer loop must return or raise"
                                )
                except TimeoutError as cause:
                    if not timeout.expired():
                        raise
                    raise DirectAnswerError(cause=cause) from cause
            except DirectAnswerError as error:
                recording.report_outcome(
                    DirectAnswerFailed(
                        failure_code=error.code,
                        attempt_count=attempt_number,
                    )
                )
                raise

            recording.report_outcome(
                DirectAnswerSucceeded(attempt_count=attempt_number)
            )
            return draft

    async def _generate_draft(
        self,
        *,
        streamer: LlmTextStreamer,
        input: DirectAnswerInput,
        attempt_number: int,
    ) -> DirectAnswerDraft:
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

                answer = strip_citation_markers("".join(raw_fragments))
                if not answer.strip():
                    raise DirectAnswerInvalidError()
                draft = self._agent.output_type(answer=answer)

                await live_draft.commit()
                return draft
        finally:
            await close_answer_stream(stream)
