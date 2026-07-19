"""AnsweringRunnerの実行境界とspan契約テスト。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import logfire
import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.contract import AnswerGenerationStopped
from app.agent.planning.contract import (
    NoRetrievalPlan,
    PlanningRequest,
    QuestionPlan,
)
from app.agent.question_context import (
    AnswerRequirement,
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextService,
    QuestionContextTelemetry,
)
from app.agent.question_context.agent import QUESTION_CONTEXT_AGENT
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.threads.contracts import ThreadMessageSnapshot
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
)

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197645")
AS_OF = datetime(2026, 7, 16, 9, 30, tzinfo=UTC)
pytestmark = pytest.mark.usefixtures("capfire")


@dataclass(frozen=True, slots=True)
class _PrepareCall:
    question: str
    history: list[ThreadMessageSnapshot]
    as_of: datetime
    run_id: UUID


@dataclass(frozen=True, slots=True)
class _HookCall:
    original_question: str
    has_history: bool
    question_context: QuestionContext


class _FakeContextPreparer:
    def __init__(
        self,
        outcomes: list[QuestionContextPreparationResult | BaseException],
        *,
        events: list[str] | None = None,
        span_probe: bool = False,
    ) -> None:
        self._outcomes = outcomes
        self._events = events
        self._span_probe = span_probe
        self.calls: list[_PrepareCall] = []

    async def prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> QuestionContextPreparationResult:
        if self._span_probe:
            with logfire.span("answering_runner_prepare_probe"):
                return await self._prepare(
                    question=question,
                    history=history,
                    as_of=as_of,
                    run_id=run_id,
                )
        return await self._prepare(
            question=question,
            history=history,
            as_of=as_of,
            run_id=run_id,
        )

    async def _prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> QuestionContextPreparationResult:
        self.calls.append(
            _PrepareCall(
                question=question,
                history=history,
                as_of=as_of,
                run_id=run_id,
            )
        )
        if self._events is not None:
            self._events.append("prepare")
        outcome = self._outcomes[len(self.calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeHooks:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        error: BaseException | None = None,
        span_probe: bool = False,
    ) -> None:
        self._events = events
        self._error = error
        self._span_probe = span_probe
        self.calls: list[_HookCall] = []

    async def on_answering_context_prepared(
        self,
        *,
        original_question: str,
        has_history: bool,
        question_context: QuestionContext,
    ) -> None:
        if self._span_probe:
            with logfire.span("answering_runner_hook_probe"):
                self._record(
                    original_question=original_question,
                    has_history=has_history,
                    question_context=question_context,
                )
            return
        self._record(
            original_question=original_question,
            has_history=has_history,
            question_context=question_context,
        )

    def _record(
        self,
        *,
        original_question: str,
        has_history: bool,
        question_context: QuestionContext,
    ) -> None:
        self.calls.append(
            _HookCall(
                original_question=original_question,
                has_history=has_history,
                question_context=question_context,
            )
        )
        if self._events is not None:
            self._events.append("hook")
        if self._error is not None:
            raise self._error


class _FakePlanner:
    def __init__(
        self,
        outcomes: list[QuestionPlan | BaseException],
        *,
        events: list[str] | None = None,
        span_probe: bool = False,
    ) -> None:
        self._outcomes = outcomes
        self._events = events
        self._span_probe = span_probe
        self.calls: list[PlanningRequest] = []

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        if self._span_probe:
            with logfire.span("answering_runner_planner_probe"):
                return self._plan(request)
        return self._plan(request)

    def _plan(self, request: PlanningRequest) -> QuestionPlan:
        self.calls.append(request)
        if self._events is not None:
            self._events.append("planner")
        outcome = self._outcomes[len(self.calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _UnreachableInternalSearch:
    async def search_articles(
        self,
        _queries: object,
    ) -> list[object]:
        raise AssertionError("internal search must not be called")


class _UnreachableExternalSearch:
    async def search(
        self,
        _tasks: list[object],
        *,
        target_time_window: str | None,
        as_of: datetime,
        external: object,
    ) -> object:
        raise AssertionError(
            "external search must not be called: "
            f"{target_time_window!r} {as_of!r} {external!r}"
        )


class _UnreachableExternalRuntimeFactory:
    def activate(self) -> object:
        raise AssertionError("external runtime must not activate")


class _UnreachableEvidenceAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: str | None,
    ) -> EvidenceAnswerDraft:
        raise AssertionError(
            f"evidence answerer must not be called: {request!r} {evidence!r} "
            f"{target_time_window!r}"
        )


class _FakeDirectAnswerer:
    def __init__(
        self,
        outcomes: list[DirectAnswerDraft | BaseException],
        *,
        events: list[str] | None = None,
        span_probe: bool = False,
    ) -> None:
        self._outcomes = outcomes
        self._events = events
        self._span_probe = span_probe
        self.calls: list[tuple[AnsweringRequest, str]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
    ) -> DirectAnswerDraft:
        if self._span_probe:
            with logfire.span("answering_runner_direct_answer_probe"):
                return self._answer(request, previous_answer)
        return self._answer(request, previous_answer)

    def _answer(
        self,
        request: AnsweringRequest,
        previous_answer: str,
    ) -> DirectAnswerDraft:
        self.calls.append((request, previous_answer))
        if self._events is not None:
            self._events.append("direct_answerer")
        outcome = self._outcomes[len(self.calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _PhasesFactory:
    def __init__(
        self,
        *,
        planner: _FakePlanner,
        direct_answerer: _FakeDirectAnswerer,
        events: list[str] | None = None,
        error: BaseException | None = None,
        span_probe: bool = False,
    ) -> None:
        self._planner = planner
        self._direct_answerer = direct_answerer
        self._events = events
        self._error = error
        self._span_probe = span_probe
        self.calls = 0
        self.created: list[AnsweringPhases] = []

    def __call__(self) -> AnsweringPhases:
        if self._span_probe:
            with logfire.span("answering_runner_phases_factory_probe"):
                return self._build()
        return self._build()

    def _build(self) -> AnsweringPhases:
        self.calls += 1
        if self._events is not None:
            self._events.append("phases_factory")
        if self._error is not None:
            raise self._error
        phases = AnsweringPhases(
            planner=self._planner,
            internal_search=_UnreachableInternalSearch(),
            external_search=_UnreachableExternalSearch(),
            external_runtime_factory=_UnreachableExternalRuntimeFactory(),
            direct_answerer=self._direct_answerer,
            evidence_answerer=_UnreachableEvidenceAnswerer(),
        )
        self.created.append(phases)
        return phases


def _preparation(context: QuestionContext) -> QuestionContextPreparationResult:
    return QuestionContextPreparationResult(
        context=context,
        telemetry=QuestionContextTelemetry(),
    )


def _runner(
    preparer: object,
    phases_factory: object,
) -> AnsweringRunner:
    return AnsweringRunner(
        context_preparer=preparer,  # type: ignore[arg-type]
        phases_factory=phases_factory,  # type: ignore[arg-type]
    )


def _run_context(*, run_id: UUID = RUN_ID, as_of: datetime = AS_OF) -> RunContext:
    return RunContext(run_id=run_id, as_of=as_of)


def _direct_factory(
    *,
    answers: list[str | BaseException],
    events: list[str] | None = None,
    span_probe: bool = False,
    factory_error: BaseException | None = None,
) -> tuple[_PhasesFactory, _FakePlanner, _FakeDirectAnswerer]:
    planner = _FakePlanner(
        [NoRetrievalPlan(reason="検索不要") for _ in answers],
        events=events,
        span_probe=span_probe,
    )
    direct_answerer = _FakeDirectAnswerer(
        [
            outcome
            if isinstance(outcome, BaseException)
            else DirectAnswerDraft(answer=outcome)
            for outcome in answers
        ],
        events=events,
        span_probe=span_probe,
    )
    return (
        _PhasesFactory(
            planner=planner,
            direct_answerer=direct_answerer,
            events=events,
            error=factory_error,
            span_probe=span_probe,
        ),
        planner,
        direct_answerer,
    )


async def test_run_forwards_input_and_orders_hook_before_lazy_phases() -> None:
    events: list[str] = []
    question = "それが投資へ与える影響は？"
    history = (
        ThreadMessageSnapshot(role="user", content="NVIDIA の発表を教えて"),
        ThreadMessageSnapshot(role="assistant", content="前回の回答"),
    )
    context = QuestionContext(standalone_question="NVIDIA の発表が投資へ与える影響は？")
    preparer = _FakeContextPreparer([_preparation(context)], events=events)
    hooks = _FakeHooks(events=events)
    factory, planner, direct_answerer = _direct_factory(
        answers=["最終回答"],
        events=events,
    )
    run_context = _run_context()

    result = await _runner(preparer, factory).run(
        RunInput(question=question, history=history),
        run_context=run_context,
        hooks=hooks,
    )

    assert preparer.calls == [
        _PrepareCall(
            question=question,
            history=list(history),
            as_of=run_context.as_of,
            run_id=run_context.run_id,
        )
    ]
    assert hooks.calls == [
        _HookCall(
            original_question=question,
            has_history=True,
            question_context=context,
        )
    ]
    assert events == [
        "prepare",
        "hook",
        "phases_factory",
        "planner",
        "direct_answerer",
    ]
    assert planner.calls[0].context is context
    assert direct_answerer.calls[0] == (
        AnsweringRequest(context=context, as_of=AS_OF),
        "前回の回答",
    )
    assert direct_answerer.calls[0][0].context is context
    assert result.final_output.answer == "最終回答"
    assert result.context.run_context is run_context
    assert result.context.question_context is context


@pytest.mark.parametrize("legacy_shape", ["starting_agent", "context"])
async def test_run_rejects_legacy_call_shapes_before_side_effects(
    legacy_shape: str,
) -> None:
    preparer = _FakeContextPreparer(
        [_preparation(QuestionContext(standalone_question="整理済みの質問"))]
    )
    factory, _, _ = _direct_factory(answers=["最終回答"])
    runner = _runner(preparer, factory)

    with pytest.raises(TypeError):
        if legacy_shape == "starting_agent":
            await runner.run(  # type: ignore[call-arg]
                object(),
                RunInput(question="元の質問", history=()),
                run_context=_run_context(),
            )
        else:
            await runner.run(  # type: ignore[call-arg]
                RunInput(question="元の質問", history=()),
                context=_run_context(),
            )

    assert preparer.calls == []
    assert factory.calls == 0


async def test_real_context_service_uses_empty_previous_answer_without_assistant() -> (
    None
):
    factory, _, direct_answerer = _direct_factory(answers=["最終回答"])
    hooks = _FakeHooks()

    result = await _runner(
        QuestionContextService(
            agent=QUESTION_CONTEXT_AGENT,
            runtime_scope_factory=None,
        ),
        factory,
    ).run(
        RunInput(question="NVIDIA の直近発表は？", history=()),
        run_context=_run_context(),
        hooks=hooks,
    )

    assert direct_answerer.calls[0][0].context is result.context.question_context
    assert direct_answerer.calls[0][1] == ""
    assert result.context.previous_answer == ""
    assert hooks.calls[0].question_context is result.context.question_context


@pytest.mark.parametrize("failure_point", ["prepare", "hook", "factory"])
async def test_failure_before_planning_prevents_later_work(
    failure_point: str,
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError(f"{failure_point} failed")
    events: list[str] = []
    preparer = _FakeContextPreparer(
        [
            error
            if failure_point == "prepare"
            else _preparation(QuestionContext(standalone_question="整理済みの質問"))
        ],
        events=events,
    )
    hooks = _FakeHooks(
        events=events,
        error=error if failure_point == "hook" else None,
    )
    factory, planner, direct_answerer = _direct_factory(
        answers=["最終回答"],
        events=events,
        factory_error=error if failure_point == "factory" else None,
    )

    with pytest.raises(RuntimeError) as raised:
        await _runner(preparer, factory).run(
            RunInput(question="元の質問", history=()),
            run_context=_run_context(),
            hooks=hooks,
        )

    assert raised.value is error
    expected = {
        "prepare": ["prepare"],
        "hook": ["prepare", "hook"],
        "factory": ["prepare", "hook", "phases_factory"],
    }
    assert events == expected[failure_point]
    assert planner.calls == []
    assert direct_answerer.calls == []
    if failure_point == "factory":
        run_span = one_span_named(capfire, "agent_answering_run")
        raw_run_span = next(
            span
            for span in capfire.exporter.exported_spans
            if span.name == "agent_answering_run"
            and (span.attributes or {}).get("logfire.span_type") == "span"
        )
        assert raw_run_span.status.status_code is StatusCode.ERROR
        assert exception_event(run_span) is not None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("answer failed"), id="unexpected-answer-error"),
        pytest.param(AnswerGenerationStopped(), id="generation-stopped"),
    ],
)
async def test_phase_exception_propagates_same_instance(error: BaseException) -> None:
    context = QuestionContext(standalone_question="整理済みの質問")
    preparer = _FakeContextPreparer([_preparation(context)])
    factory, _, direct_answerer = _direct_factory(answers=[error])

    with pytest.raises(type(error)) as raised:
        await _runner(preparer, factory).run(
            RunInput(question="元の質問", history=()),
            run_context=_run_context(),
        )

    assert raised.value is error
    assert len(direct_answerer.calls) == 1


async def test_generation_stopped_closes_run_span_without_error(
    capfire: CaptureLogfire,
) -> None:
    error = AnswerGenerationStopped()
    preparer = _FakeContextPreparer(
        [_preparation(QuestionContext(standalone_question="整理済みの質問"))]
    )
    factory, _, _ = _direct_factory(answers=[error])

    with pytest.raises(AnswerGenerationStopped) as raised:
        await _runner(preparer, factory).run(
            RunInput(question="元の質問", history=()),
            run_context=_run_context(),
        )

    span = one_span_named(capfire, "agent_answering_run")
    assert raised.value is error
    assert exception_event(span) is None
    assert span["attributes"].get("logfire.level_num", 0) < 17


async def test_same_runner_reprepares_and_builds_fresh_phases_per_run() -> None:
    first_context = QuestionContext(standalone_question="最初の整理済み質問")
    second_context = QuestionContext(standalone_question="次の整理済み質問")
    preparer = _FakeContextPreparer(
        [_preparation(first_context), _preparation(second_context)]
    )
    factory, _, direct_answerer = _direct_factory(answers=["最初の回答", "次の回答"])
    runner = _runner(preparer, factory)

    first = await runner.run(
        RunInput(question="最初の質問", history=()),
        run_context=_run_context(),
    )
    second = await runner.run(
        RunInput(question="次の質問", history=()),
        run_context=_run_context(
            run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197646"),
            as_of=datetime(2026, 7, 16, 9, 31, tzinfo=UTC),
        ),
    )

    assert [call.question for call in preparer.calls] == ["最初の質問", "次の質問"]
    assert factory.calls == 2
    assert factory.created[0] is not factory.created[1]
    assert first.context is not second.context
    assert first.context.question_context is first_context
    assert second.context.question_context is second_context
    assert direct_answerer.calls[0][0].context is first_context
    assert direct_answerer.calls[1][0].context is second_context


async def test_run_span_wraps_prepare_hook_factory_and_phases_under_parent(
    capfire: CaptureLogfire,
) -> None:
    events: list[str] = []
    context = QuestionContext(standalone_question="整理済みの質問")
    preparer = _FakeContextPreparer(
        [_preparation(context)],
        events=events,
        span_probe=True,
    )
    hooks = _FakeHooks(events=events, span_probe=True)
    factory, _, _ = _direct_factory(
        answers=["最終回答"],
        events=events,
        span_probe=True,
    )

    with logfire.span("answering_runner_parent_probe"):
        await _runner(preparer, factory).run(
            RunInput(question="元の質問", history=()),
            run_context=_run_context(),
            hooks=hooks,
        )

    parent = one_span_named(capfire, "answering_runner_parent_probe")
    answering_run = one_span_named(capfire, "agent_answering_run")
    assert answering_run["parent"]["span_id"] == parent["context"]["span_id"]
    assert answering_run["context"]["trace_id"] == parent["context"]["trace_id"]
    assert events == [
        "prepare",
        "hook",
        "phases_factory",
        "planner",
        "direct_answerer",
    ]

    for probe_name in (
        "answering_runner_prepare_probe",
        "answering_runner_hook_probe",
        "answering_runner_phases_factory_probe",
        "answering_runner_planner_probe",
        "answering_runner_direct_answer_probe",
    ):
        probe = one_span_named(capfire, probe_name)
        assert probe["parent"]["span_id"] == answering_run["context"]["span_id"]
        assert probe["context"]["trace_id"] == answering_run["context"]["trace_id"]


async def test_run_span_attributes_do_not_include_model_visible_text(
    capfire: CaptureLogfire,
) -> None:
    sentinels = {
        "raw_question": "RAW_QUESTION_SENTINEL_5a3f",
        "user_history": "USER_HISTORY_SENTINEL_b972",
        "previous_answer": "PREVIOUS_ANSWER_SENTINEL_83c1",
        "standalone_question": "STANDALONE_QUESTION_SENTINEL_27de",
        "content_prompt": "CONTENT_PROMPT_SENTINEL_6fb4",
        "response_prompt": "RESPONSE_PROMPT_SENTINEL_a104",
        "prior_coverage": "PRIOR_COVERAGE_SENTINEL_d538",
        "active_goal": "ACTIVE_GOAL_SENTINEL_72af",
        "final_answer": "FINAL_ANSWER_SENTINEL_c691",
    }
    context = QuestionContext(
        standalone_question=sentinels["standalone_question"],
        content_requirements=[
            AnswerRequirement(
                requirement_id="c1",
                description=sentinels["content_prompt"],
            )
        ],
        response_requirements=[
            AnswerRequirement(
                requirement_id="p1",
                description=sentinels["response_prompt"],
            )
        ],
        relevant_prior_coverage=sentinels["prior_coverage"],
        active_goal=sentinels["active_goal"],
    )
    history = (
        ThreadMessageSnapshot(role="user", content=sentinels["user_history"]),
        ThreadMessageSnapshot(
            role="assistant",
            content=sentinels["previous_answer"],
        ),
    )
    preparer = _FakeContextPreparer([_preparation(context)])
    factory, _, _ = _direct_factory(answers=[sentinels["final_answer"]])

    await _runner(preparer, factory).run(
        RunInput(question=sentinels["raw_question"], history=history),
        run_context=_run_context(),
    )

    attributes = one_span_named(capfire, "agent_answering_run")["attributes"]
    attributes_dump = json.dumps(attributes, ensure_ascii=False, default=str)
    assert domain_attr_keys(attributes) == {"run_id"}
    assert attributes["run_id"] == str(RUN_ID)
    assert all(value not in attributes_dump for value in sentinels.values())
