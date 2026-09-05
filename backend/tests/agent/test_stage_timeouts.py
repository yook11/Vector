"""工程の時間枠、再試行、外部キャンセルの境界を検証する。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.answering.direct_answer.service import DirectAnswerService
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.answering.evidence_answer.contract import EvidenceAnswerInput
from app.agent.answering.evidence_answer.failure import EvidenceAnswerError
from app.agent.answering.evidence_answer.service import EvidenceAnswerService
from app.agent.evidence_review import EvidenceReviewService, EvidenceRunFailed
from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.planning.failure import PlanningError
from app.agent.planning.service import QuestionPlanningService
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderOutputTruncatedError
from tests.agent.answering.direct_answer.test_service import _input as direct_input
from tests.agent.answering.evidence_answer.test_service import (
    RecordingDeltaReporter,
    _evidence,
    _request,
)
from tests.agent.evidence_review.test_service import (
    _ANY_REVIEWER_DRAFT,
    AS_OF,
    _internal_task,
)
from tests.agent.planning.test_planner import _draft
from tests.agent.planning.test_planner import _input as planning_input
from tests.agent.recording._fakes import (
    RecordingDirectAnswerRecorder,
    RecordingEvidenceAnswerRecorder,
    RecordingEvidenceReviewRecorder,
    RecordingPlanningRecorder,
)
from tests.agent.running._harness import AllowAnswerGenerationStart

STAGES = ("planning", "review", "direct", "evidence")
MODULES = {
    "planning": ("app.agent.planning.service", "_PLANNING_TIMEOUT_SECONDS"),
    "review": ("app.agent.evidence_review.service", "_REVIEW_TIMEOUT_SECONDS"),
    "direct": ("app.agent.answering.direct_answer.service", "_ANSWER_TIMEOUT_SECONDS"),
    "evidence": (
        "app.agent.answering.evidence_answer.service",
        "_ANSWER_TIMEOUT_SECONDS",
    ),
}
CODES = {
    "planning": "planning_timeout",
    "review": "reviewer_timeout",
    "direct": "direct_answer_timeout",
    "evidence": "evidence_answer_timeout",
}
ERRORS = {
    "planning": PlanningError,
    "direct": DirectAnswerError,
    "evidence": EvidenceAnswerError,
}


class WaitingRuntime:
    def __init__(self, stage: str, mode: str) -> None:
        self.stage = stage
        self.mode = mode
        self.calls: list[int] = []
        self.started = asyncio.Event()
        self.stream_closed = False

    async def call(self, agent: Any, input: Any, *, attempt_number: int) -> Any:
        self.calls.append(attempt_number)
        self.started.set()
        if self.mode == "retry":
            await asyncio.sleep(0.12)
            if attempt_number == 1:
                raise AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)
            return (
                _draft(plan_type="direct_answer")
                if self.stage == "planning"
                else _ANY_REVIEWER_DRAFT
            )
        await asyncio.Event().wait()

    async def stream_text(self, agent: Any, input: Any, *, attempt_number: int):
        self.calls.append(attempt_number)
        self.started.set()
        try:
            if self.mode == "retry":
                await asyncio.sleep(0.12)
                if attempt_number == 1:
                    raise AIProviderOutputTruncatedError()
                yield "回答です。[[1]]"
            elif self.mode == "fragments":
                while True:
                    yield "生成中の本文。"
                    await asyncio.sleep(0.001)
            else:
                await asyncio.Event().wait()
        finally:
            self.stream_closed = True


def _stage(stage: str, mode: str = "blocked") -> SimpleNamespace:
    runtime = WaitingRuntime(stage, mode)
    state = SimpleNamespace(
        runtime=runtime, closed=False, delta=RecordingDeltaReporter()
    )

    @asynccontextmanager
    async def scope():
        try:
            if mode == "setup":
                runtime.started.set()
                await asyncio.Event().wait()
            yield runtime
        finally:
            state.closed = True

    if stage == "planning":
        state.recorder = RecordingPlanningRecorder()
        service = QuestionPlanningService(
            agent=QUESTION_PLANNER_AGENT,
            runtime_scope_factory=scope,
            recorder=state.recorder,
        )
        state.run = lambda: service.plan(planning_input())
    elif stage == "review":
        state.recorder = RecordingEvidenceReviewRecorder()
        service = EvidenceReviewService(
            agent=EVIDENCE_REVIEWER_AGENT,
            runtime_scope_factory=scope,
            recorder=state.recorder,
        )
        state.run = lambda: service.review(tasks=[_internal_task()], as_of=AS_OF)
    elif stage == "direct":
        state.recorder = RecordingDirectAnswerRecorder()
        service = DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=scope,
            repository=AllowAnswerGenerationStart(),
            recorder=state.recorder,
            delta_reporter=state.delta,
        )
        state.run = lambda: service.answer(direct_input())
    else:
        state.recorder = RecordingEvidenceAnswerRecorder()
        service = EvidenceAnswerService(
            agent=EVIDENCE_ANSWER_AGENT,
            runtime_scope_factory=scope,
            repository=AllowAnswerGenerationStart(),
            recorder=state.recorder,
            delta_reporter=state.delta,
        )
        state.run = lambda: service.answer(
            EvidenceAnswerInput(
                request=_request(),
                evidence=(_evidence(),),
                target_time_window=None,
                review_missing=(),
            )
        )
    return state


async def _assert_timeout(stage: str, state: SimpleNamespace) -> None:
    if stage == "review":
        result = await state.run()
        assert isinstance(result, EvidenceRunFailed)
        assert result.failure_code == CODES[stage]
    else:
        with pytest.raises(ERRORS[stage]) as raised:
            await state.run()
        assert raised.value.code == CODES[stage]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("mode", ["setup", "blocked", "retry"])
async def test_stage_budget_includes_setup_and_retries(
    stage: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, constant = MODULES[stage]
    monkeypatch.setattr(f"{module}.{constant}", 0.2 if mode == "retry" else 0.02)
    state = _stage(stage, mode)

    await _assert_timeout(stage, state)

    expected_attempts = 0 if mode == "setup" else 2 if mode == "retry" else 1
    assert state.runtime.calls == list(range(1, expected_attempts + 1))
    assert state.closed
    outcomes = state.recorder.records[0].outcomes
    assert len(outcomes) == 1
    assert (outcomes[0].failure_code, outcomes[0].attempt_count) == (
        CODES[stage],
        expected_attempts,
    )
    if stage in ERRORS and stage != "planning" and mode != "setup":
        assert state.runtime.stream_closed
        assert state.delta.aborted[-1] == expected_attempts
        assert state.delta.finished == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["direct", "evidence"])
async def test_continuous_fragments_do_not_extend_answer_budget(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, constant = MODULES[stage]
    monkeypatch.setattr(f"{module}.{constant}", 0.03)
    state = _stage(stage, "fragments")

    await _assert_timeout(stage, state)

    assert len(state.delta.appended) > 1
    assert state.delta.aborted == [1]
    assert state.delta.finished == []
    assert state.runtime.stream_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
async def test_external_cancellation_is_not_a_stage_timeout(stage: str) -> None:
    state = _stage(stage)
    task = asyncio.create_task(state.run())
    await asyncio.wait_for(state.runtime.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.closed
    assert state.recorder.records[0].outcomes == []


def test_agreed_runtime_budgets() -> None:
    from importlib import import_module

    from app.agent.evidence_collection.external_search.policy import (
        PROVIDER_SEARCH_TIMEOUT_SECONDS,
        QUERY_GENERATE_TIMEOUT_SECONDS,
    )
    from app.agent.evidence_collection.internal_search.service import (
        _INTERNAL_SEARCH_TIMEOUT_SECONDS,
    )
    from app.agent.runs.repository import _ANSWER_SAVE_LOCK_TIMEOUT

    assert [
        getattr(import_module(module), constant)
        for module, constant in MODULES.values()
    ] == [15, 15, 15, 15]
    assert (
        _INTERNAL_SEARCH_TIMEOUT_SECONDS,
        QUERY_GENERATE_TIMEOUT_SECONDS,
        PROVIDER_SEARCH_TIMEOUT_SECONDS,
        _ANSWER_SAVE_LOCK_TIMEOUT,
    ) == (15, 10, 15, "10s")
