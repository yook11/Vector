"""AnsweringRunner の pure application behavior tests。"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any
from uuid import UUID

import logfire
import pytest
from logfire.testing import CaptureLogfire

from app.agent.contract import (
    AnswerGenerationStopped,
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
)
from app.agent.question_context import (
    AnswerRequirement,
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextService,
    QuestionContextTelemetry,
)
from app.agent.running import RunContext, RunInput
from app.agent.threads.contracts import ThreadMessageSnapshot
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
)

ANSWERING_RUNNER_MODULE = "app.agent.running.answering_runner"
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
    ) -> None:
        self._outcomes = outcomes
        self._events = events
        self.calls: list[_PrepareCall] = []

    async def prepare(
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


class _FakeStartingAgent:
    def __init__(
        self,
        outcomes: list[AnswerQuestionResult | BaseException],
        *,
        events: list[str] | None = None,
    ) -> None:
        self._outcomes = outcomes
        self._events = events
        self.calls: list[AnswerQuestionInput] = []

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult:
        self.calls.append(input)
        if self._events is not None:
            self._events.append("agent")
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
    ) -> None:
        self._events = events
        self._error = error
        self.calls: list[_HookCall] = []

    async def on_answering_context_prepared(
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


class _SpanProbeContextPreparer(_FakeContextPreparer):
    async def prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> QuestionContextPreparationResult:
        with logfire.span("answering_runner_prepare_probe"):
            return await super().prepare(
                question=question,
                history=history,
                as_of=as_of,
                run_id=run_id,
            )


class _SpanProbeHooks(_FakeHooks):
    async def on_answering_context_prepared(
        self,
        *,
        original_question: str,
        has_history: bool,
        question_context: QuestionContext,
    ) -> None:
        with logfire.span("answering_runner_hook_probe"):
            await super().on_answering_context_prepared(
                original_question=original_question,
                has_history=has_history,
                question_context=question_context,
            )


class _SpanProbeStartingAgent(_FakeStartingAgent):
    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult:
        with logfire.span("answering_runner_agent_probe"):
            return await super().answer(input)


def _answering_runner_module() -> ModuleType:
    missing_answering_runner = False
    try:
        return importlib.import_module(ANSWERING_RUNNER_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == ANSWERING_RUNNER_MODULE or exc.name.startswith(
            f"{ANSWERING_RUNNER_MODULE}."
        ):
            missing_answering_runner = True
        else:
            raise
    if missing_answering_runner:
        pytest.fail(
            "app.agent.running.answering_runner.AnsweringRunner が未実装です",
            pytrace=False,
        )
    raise AssertionError("unreachable")


def _answering_runner(context_preparer: object) -> Any:
    answering_runner_type = getattr(
        _answering_runner_module(),
        "AnsweringRunner",
        None,
    )
    if answering_runner_type is None:
        pytest.fail(
            "app.agent.running.answering_runner must define AnsweringRunner",
            pytrace=False,
        )
    return answering_runner_type(context_preparer=context_preparer)


def _run_context(*, run_id: UUID = RUN_ID, as_of: datetime = AS_OF) -> RunContext:
    return RunContext(run_id=run_id, as_of=as_of)


def _preparation(question_context: QuestionContext) -> QuestionContextPreparationResult:
    return QuestionContextPreparationResult(
        context=question_context,
        telemetry=QuestionContextTelemetry(),
    )


def _answer_result(answer: str = "最終回答") -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer=answer,
        retrieval=AnswerRetrievalSummary(planned_mode="none"),
    )


async def test_run_forwards_input_once_and_calls_hook_before_agent() -> None:
    events: list[str] = []
    question = "それが投資へ与える影響は？"
    history = (
        ThreadMessageSnapshot(role="user", content="NVIDIA の発表を教えて"),
        ThreadMessageSnapshot(role="assistant", content="前回の回答"),
    )
    question_context = QuestionContext(
        standalone_question="NVIDIA の発表が投資へ与える影響は？"
    )
    preparer = _FakeContextPreparer(
        [_preparation(question_context)],
        events=events,
    )
    hooks = _FakeHooks(events=events)
    agent = _FakeStartingAgent([_answer_result()], events=events)
    run_context = _run_context()

    await _answering_runner(preparer).run(
        agent,
        RunInput(question=question, history=history),
        run_context=run_context,
        hooks=hooks,
    )

    assert (
        preparer.calls,
        isinstance(preparer.calls[0].history, list),
        hooks.calls,
        hooks.calls[0].question_context is question_context,
        events,
    ) == (
        [
            _PrepareCall(
                question=question,
                history=list(history),
                as_of=run_context.as_of,
                run_id=run_context.run_id,
            )
        ],
        True,
        [
            _HookCall(
                original_question=question,
                has_history=True,
                question_context=question_context,
            )
        ],
        True,
        ["prepare", "hook", "agent"],
    )


async def test_run_rejects_legacy_context_keyword_before_side_effects() -> None:
    preparer = _FakeContextPreparer(
        [_preparation(QuestionContext(standalone_question="整理済みの質問"))]
    )
    agent = _FakeStartingAgent([_answer_result()])

    with pytest.raises(TypeError):
        await _answering_runner(preparer).run(
            agent,
            RunInput(question="元の質問", history=()),
            context=_run_context(),
        )

    assert (preparer.calls, agent.calls) == ([], [])


async def test_run_projects_latest_assistant_and_returns_same_output_and_context() -> (
    None
):
    history = (
        ThreadMessageSnapshot(role="assistant", content="古い回答"),
        ThreadMessageSnapshot(role="user", content="追加の質問"),
        ThreadMessageSnapshot(role="assistant", content="  最新の回答本文\n"),
        ThreadMessageSnapshot(role="user", content="さらに確認"),
    )
    question_context = QuestionContext(standalone_question="整理済みの質問")
    preparer = _FakeContextPreparer([_preparation(question_context)])
    final_output = _answer_result()
    agent = _FakeStartingAgent([final_output])
    run_context = _run_context()

    result = await _answering_runner(preparer).run(
        agent,
        RunInput(question="元の質問", history=history),
        run_context=run_context,
    )

    assert (
        len(agent.calls),
        agent.calls[0].context is question_context,
        agent.calls[0].as_of,
        agent.calls[0].previous_answer,
        result.final_output is final_output,
        result.context.run_context is run_context,
        result.context.question_context is question_context,
        result.context.previous_answer,
    ) == (
        1,
        True,
        result.context.run_context.as_of,
        "  最新の回答本文\n",
        True,
        True,
        True,
        agent.calls[0].previous_answer,
    )


async def test_real_context_service_uses_empty_previous_answer_without_assistant() -> (
    None
):
    question = "NVIDIA の直近発表は？"
    agent = _FakeStartingAgent([_answer_result()])
    hooks = _FakeHooks()

    result = await _answering_runner(QuestionContextService(generator=None)).run(
        agent,
        RunInput(question=question, history=()),
        run_context=_run_context(),
        hooks=hooks,
    )

    assert (
        len(agent.calls),
        agent.calls[0].context is result.context.question_context,
        result.context.question_context.standalone_question,
        agent.calls[0].previous_answer,
        result.context.previous_answer,
        hooks.calls,
        hooks.calls[0].question_context is result.context.question_context,
    ) == (
        1,
        True,
        question,
        "",
        "",
        [
            _HookCall(
                original_question=question,
                has_history=False,
                question_context=result.context.question_context,
            )
        ],
        True,
    )


async def test_preparer_exception_propagates_without_hook_or_agent_call() -> None:
    error = RuntimeError("preparation failed")
    events: list[str] = []
    preparer = _FakeContextPreparer([error], events=events)
    hooks = _FakeHooks(events=events)
    agent = _FakeStartingAgent([_answer_result()], events=events)

    with pytest.raises(RuntimeError) as raised:
        await _answering_runner(preparer).run(
            agent,
            RunInput(question="質問", history=()),
            run_context=_run_context(),
            hooks=hooks,
        )

    assert (
        raised.value is error,
        len(preparer.calls),
        hooks.calls,
        agent.calls,
        events,
    ) == (True, 1, [], [], ["prepare"])


async def test_hook_exception_propagates_and_prevents_agent_call() -> None:
    error = RuntimeError("hook failed")
    events: list[str] = []
    preparer = _FakeContextPreparer(
        [_preparation(QuestionContext(standalone_question="整理済みの質問"))],
        events=events,
    )
    hooks = _FakeHooks(events=events, error=error)
    agent = _FakeStartingAgent([_answer_result()], events=events)

    with pytest.raises(RuntimeError) as raised:
        await _answering_runner(preparer).run(
            agent,
            RunInput(question="元の質問", history=()),
            run_context=_run_context(),
            hooks=hooks,
        )

    assert (
        raised.value is error,
        len(preparer.calls),
        len(hooks.calls),
        agent.calls,
        events,
    ) == (True, 1, 1, [], ["prepare", "hook"])


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("agent failed"), id="unexpected-agent-error"),
        pytest.param(AnswerGenerationStopped(), id="generation-stopped"),
    ],
)
async def test_agent_exception_propagates_without_retry(error: BaseException) -> None:
    events: list[str] = []
    preparer = _FakeContextPreparer(
        [_preparation(QuestionContext(standalone_question="整理済みの質問"))],
        events=events,
    )
    hooks = _FakeHooks(events=events)
    agent = _FakeStartingAgent([error], events=events)

    with pytest.raises(type(error)) as raised:
        await _answering_runner(preparer).run(
            agent,
            RunInput(question="元の質問", history=()),
            run_context=_run_context(),
            hooks=hooks,
        )

    assert (
        raised.value is error,
        len(preparer.calls),
        len(hooks.calls),
        len(agent.calls),
        events,
    ) == (True, 1, 1, 1, ["prepare", "hook", "agent"])


async def test_generation_stopped_closes_run_span_without_error(
    capfire: CaptureLogfire,
) -> None:
    error = AnswerGenerationStopped()
    preparer = _FakeContextPreparer(
        [_preparation(QuestionContext(standalone_question="整理済みの質問"))]
    )
    agent = _FakeStartingAgent([error])

    with pytest.raises(AnswerGenerationStopped) as raised:
        await _answering_runner(preparer).run(
            agent,
            RunInput(question="元の質問", history=()),
            run_context=_run_context(),
        )

    span = one_span_named(capfire, "agent_answering_run")
    assert raised.value is error
    assert exception_event(span) is None
    assert span["attributes"].get("logfire.level_num", 0) < 17


async def test_same_answering_runner_reprepares_and_builds_fresh_context() -> None:
    first_question_context = QuestionContext(standalone_question="最初の整理済み質問")
    second_question_context = QuestionContext(standalone_question="次の整理済み質問")
    preparer = _FakeContextPreparer(
        [
            _preparation(first_question_context),
            _preparation(second_question_context),
        ]
    )
    agent = _FakeStartingAgent(
        [_answer_result("最初の回答"), _answer_result("次の回答")]
    )
    answering_runner = _answering_runner(preparer)

    first_result = await answering_runner.run(
        agent,
        RunInput(question="最初の質問", history=()),
        run_context=_run_context(),
    )
    second_result = await answering_runner.run(
        agent,
        RunInput(question="次の質問", history=()),
        run_context=_run_context(
            run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197646"),
            as_of=datetime(2026, 7, 16, 9, 31, tzinfo=UTC),
        ),
    )

    assert (
        len(preparer.calls),
        [call.question for call in preparer.calls],
        len(agent.calls),
        first_result.context is not second_result.context,
        first_result.context.question_context is first_question_context,
        second_result.context.question_context is second_question_context,
        agent.calls[0].context is first_question_context,
        agent.calls[1].context is second_question_context,
    ) == (2, ["最初の質問", "次の質問"], 2, True, True, True, True, True)


async def test_run_span_wraps_prepare_hook_and_agent_under_current_parent(
    capfire: CaptureLogfire,
) -> None:
    events: list[str] = []
    question_context = QuestionContext(standalone_question="整理済みの質問")
    preparer = _SpanProbeContextPreparer(
        [_preparation(question_context)],
        events=events,
    )
    hooks = _SpanProbeHooks(events=events)
    agent = _SpanProbeStartingAgent([_answer_result()], events=events)

    with logfire.span("answering_runner_parent_probe"):
        await _answering_runner(preparer).run(
            agent,
            RunInput(question="元の質問", history=()),
            run_context=_run_context(),
            hooks=hooks,
        )

    parent = one_span_named(capfire, "answering_runner_parent_probe")
    answering_run = one_span_named(capfire, "agent_answering_run")
    assert (
        answering_run["parent"]["span_id"],
        answering_run["context"]["trace_id"],
        events,
    ) == (
        parent["context"]["span_id"],
        parent["context"]["trace_id"],
        ["prepare", "hook", "agent"],
    )

    for probe_name in (
        "answering_runner_prepare_probe",
        "answering_runner_hook_probe",
        "answering_runner_agent_probe",
    ):
        probe = one_span_named(capfire, probe_name)
        assert (
            probe["parent"]["span_id"],
            probe["context"]["trace_id"],
        ) == (
            answering_run["context"]["span_id"],
            answering_run["context"]["trace_id"],
        )


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
    question_context = QuestionContext(
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

    await _answering_runner(_FakeContextPreparer([_preparation(question_context)])).run(
        _FakeStartingAgent([_answer_result(sentinels["final_answer"])]),
        RunInput(question=sentinels["raw_question"], history=history),
        run_context=_run_context(),
    )

    attributes = one_span_named(capfire, "agent_answering_run")["attributes"]
    attributes_dump = json.dumps(attributes, ensure_ascii=False, default=str)
    assert domain_attr_keys(attributes) == {"run_id"}
    assert attributes["run_id"] == str(RUN_ID)
    assert all(value not in attributes_dump for value in sentinels.values())
