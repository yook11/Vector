"""AnsweringRunnerの実行境界とspan契約テスト。"""

from __future__ import annotations

import json
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
from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_review import EvidenceReviewer
from app.agent.planning.contract import (
    DirectAnswerPlan,
    PlanningRequest,
    QuestionPlan,
    TargetTimeWindow,
)
from app.agent.running import (
    AnsweringPhases,
    AnsweringRunner,
    RunIdentity,
    RunInput,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from tests.agent.running._harness import (
    THREAD_ID,
    USER_ID,
    PassThroughOrganizer,
)
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
)

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197645")
AS_OF = datetime(2026, 7, 16, 9, 30, tzinfo=UTC)
pytestmark = pytest.mark.usefixtures("capfire")


def _direct_plan() -> DirectAnswerPlan:
    return DirectAnswerPlan()


class _FakeProgressReporter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def stage_changed(self, stage: str) -> None:
        self.calls.append(stage)


class _FakeEventReporter:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def event_occurred(self, event: object) -> None:
        self.events.append(event)


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
    async def search(self, queries: object) -> list[object]:
        raise AssertionError(f"internal search must not be called: {queries!r}")


class _UnreachableExternalSearchScope:
    def __call__(self) -> object:
        raise AssertionError("external scope must not activate")


class _UnreachableEvidenceAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerDraft:
        raise AssertionError(
            f"evidence answerer must not be called: {request!r} {evidence!r} "
            f"{target_time_window!r} {review_missing!r}"
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
            collector=EvidenceCollectionService(
                internal_search=_UnreachableInternalSearch(),
                external_search_scope_factory=_UnreachableExternalSearchScope(),
            ),
            direct_answerer=self._direct_answerer,
            evidence_answerer=_UnreachableEvidenceAnswerer(),
            reviewer=EvidenceReviewer(
                runtime_scope_factory=_UnreachableExternalSearchScope(),
            ),
            organizer=PassThroughOrganizer(),
        )
        self.created.append(phases)
        return phases


def _runner(
    phases_factory: object,
    *,
    progress: object | None = None,
    events: object | None = None,
) -> AnsweringRunner:
    return AnsweringRunner(
        phases_factory=phases_factory,  # type: ignore[arg-type]
        progress=progress,  # type: ignore[arg-type]
        events=events,  # type: ignore[arg-type]
    )


def _run_identity(*, run_id: UUID = RUN_ID, as_of: datetime = AS_OF) -> RunIdentity:
    return RunIdentity(
        user_id=USER_ID,
        run_id=run_id,
        thread_id=THREAD_ID,
        as_of=as_of,
    )


def _direct_factory(
    *,
    answers: list[str | BaseException],
    events: list[str] | None = None,
    span_probe: bool = False,
    factory_error: BaseException | None = None,
) -> tuple[_PhasesFactory, _FakePlanner, _FakeDirectAnswerer]:
    planner = _FakePlanner(
        [_direct_plan() for _ in answers],
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


async def test_direct_path_reports_no_internal_or_external_search_events() -> None:
    reporter = _FakeEventReporter()
    factory, _, _ = _direct_factory(answers=["最終回答"])

    await _runner(factory, events=reporter).run(
        RunInput(question="元の質問", history=()),
        identity=_run_identity(),
    )

    assert reporter.events == []


async def test_direct_answer_gets_empty_previous_answer_without_assistant() -> None:
    factory, _, direct_answerer = _direct_factory(answers=["最終回答"])

    await _runner(factory).run(
        RunInput(question="NVIDIA の直近発表は？", history=()),
        identity=_run_identity(),
    )

    assert direct_answerer.calls[0][0].question == "NVIDIA の直近発表は？"
    assert direct_answerer.calls[0][1] == ""


async def test_failure_before_planning_prevents_later_work(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("factory failed")
    events: list[str] = []
    factory, planner, direct_answerer = _direct_factory(
        answers=["最終回答"],
        events=events,
        factory_error=error,
    )

    with pytest.raises(RuntimeError) as raised:
        await _runner(factory).run(
            RunInput(question="元の質問", history=()),
            identity=_run_identity(),
        )

    assert raised.value is error
    assert events == ["phases_factory"]
    assert planner.calls == []
    assert direct_answerer.calls == []
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
    factory, _, direct_answerer = _direct_factory(answers=[error])

    with pytest.raises(type(error)) as raised:
        await _runner(factory).run(
            RunInput(question="元の質問", history=()),
            identity=_run_identity(),
        )

    assert raised.value is error
    assert len(direct_answerer.calls) == 1


async def test_generation_stopped_closes_run_span_without_error(
    capfire: CaptureLogfire,
) -> None:
    error = AnswerGenerationStopped()
    factory, _, _ = _direct_factory(answers=[error])

    with pytest.raises(AnswerGenerationStopped) as raised:
        await _runner(factory).run(
            RunInput(question="元の質問", history=()),
            identity=_run_identity(),
        )

    span = one_span_named(capfire, "agent_answering_run")
    assert raised.value is error
    assert exception_event(span) is None
    assert span["attributes"].get("logfire.level_num", 0) < 17


async def test_same_runner_builds_fresh_phases_per_run() -> None:
    factory, _, direct_answerer = _direct_factory(answers=["最初の回答", "次の回答"])
    runner = _runner(factory)

    await runner.run(
        RunInput(question="最初の質問", history=()),
        identity=_run_identity(),
    )
    await runner.run(
        RunInput(question="次の質問", history=()),
        identity=_run_identity(
            run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197646"),
            as_of=datetime(2026, 7, 16, 9, 31, tzinfo=UTC),
        ),
    )

    assert factory.calls == 2
    assert factory.created[0] is not factory.created[1]
    assert [call[0].question for call in direct_answerer.calls] == [
        "最初の質問",
        "次の質問",
    ]


async def test_run_span_wraps_factory_and_phases_under_parent(
    capfire: CaptureLogfire,
) -> None:
    events: list[str] = []
    factory, _, _ = _direct_factory(
        answers=["最終回答"],
        events=events,
        span_probe=True,
    )

    with logfire.span("answering_runner_parent_probe"):
        await _runner(factory).run(
            RunInput(question="元の質問", history=()),
            identity=_run_identity(),
        )

    parent = one_span_named(capfire, "answering_runner_parent_probe")
    answering_run = one_span_named(capfire, "agent_answering_run")
    assert answering_run["parent"]["span_id"] == parent["context"]["span_id"]
    assert answering_run["context"]["trace_id"] == parent["context"]["trace_id"]
    assert events == [
        "phases_factory",
        "planner",
        "direct_answerer",
    ]

    for probe_name in (
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
        "final_answer": "FINAL_ANSWER_SENTINEL_c691",
    }
    history = (
        ThreadMessageSnapshot(role="user", content=sentinels["user_history"]),
        ThreadMessageSnapshot(
            role="assistant",
            content=sentinels["previous_answer"],
        ),
    )
    factory, _, _ = _direct_factory(answers=[sentinels["final_answer"]])

    await _runner(factory).run(
        RunInput(question=sentinels["raw_question"], history=history),
        identity=_run_identity(),
    )

    attributes = one_span_named(capfire, "agent_answering_run")["attributes"]
    attributes_dump = json.dumps(attributes, ensure_ascii=False, default=str)
    assert domain_attr_keys(attributes) == {"run_id"}
    assert attributes["run_id"] == str(RUN_ID)
    assert all(value not in attributes_dump for value in sentinels.values())
