"""渡された根拠だけを引用する回答を生成する工程。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

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
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerProgressReporter,
)
from app.agent.llm_call.caller import (
    AgentTextStream,
    LlmTextStreamer,
    LlmTextStreamerScopeFactory,
)
from app.agent.recording.evidence_answer import (
    EvidenceAnswerFailed,
    EvidenceAnswerRecorder,
    EvidenceAnswerSucceeded,
    logfire_evidence_answer_recorder,
)
from app.agent.retry_prospect import is_resolvable_by_retry
from app.agent.running.answer_generation import (
    AnswerGenerationRepository,
)
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
        repository: AnswerGenerationRepository,
        schedule_deadline_check: Callable[[UUID, datetime], None],
        delta_reporter: AnswerDeltaReporter | None = None,
        progress: AnswerProgressReporter | None = None,
        recorder: EvidenceAnswerRecorder = logfire_evidence_answer_recorder,
    ) -> None:
        self._agent = agent
        self._llm_text_streamer_scope_factory = llm_text_streamer_scope_factory
        self._repository = repository
        self._schedule_deadline_check = schedule_deadline_check
        self._delta = BestEffortAnswerDeltaReporter(delta_reporter)
        self._progress = progress
        self._recorder = recorder

    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerDraft:
        """再試行を含む時間枠内でdraftを完成させ、生成不能は例外で通知する。"""

        result = await self._repository.start_answer_generation()

        timeout = asyncio.timeout(answer_timing.ANSWER_GENERATION_TIMEOUT_SECONDS)
        async with self._recorder.record(agent_name=self._agent.name) as recording:
            attempt_number = 0
            try:
                try:
                    async with timeout:
                        self._schedule_deadline_check(
                            result.run_id,
                            result.answer_started_at
                            + answer_timing.answer_generation_recovery_window(),
                        )
                        if self._progress is not None:
                            await self._progress.stage_changed("answering")
                        async with self._llm_text_streamer_scope_factory() as streamer:
                            for attempt_number in range(1, _MAX_ATTEMPTS + 1):
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
                                    await self._start_revision(
                                        generation=attempt_number + 1
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

    async def _start_revision(self, *, generation: int) -> None:
        await self._repository.authorize_answer_regeneration()
        await self._delta.reset(generation=generation)
