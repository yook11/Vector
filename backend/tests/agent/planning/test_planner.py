"""QuestionPlanningService の2 plan retry・failure・metric contract。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.planning.contract import (
    PlanningRequest,
    QuestionPlanDraft,
    ResearchTaskDraft,
    TargetTimeWindow,
)
from app.agent.planning.service import QuestionPlanningService
from app.agent.recording.planning import (
    PlanningFailed,
    PlanningOutcome,
    PlanningSucceeded,
)
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from tests.agent.recording._fakes import RecordingPlanningRecorder
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_PLANNER_OUTCOME_METRIC = "vector.agent.planner.outcome"


def _input(question: str = "今日のNVIDIAの発表は？") -> PlanningRequest:
    return PlanningRequest(
        question=question,
        as_of=datetime(2026, 7, 20, tzinfo=UTC),
    )


def _draft(
    *,
    plan_type: str,
    research_tasks: list[Any] | None = None,
    target_time_window: object | None = None,
) -> QuestionPlanDraft:
    return QuestionPlanDraft(
        plan_type=plan_type,
        research_tasks=research_tasks or [],
        target_time_window=target_time_window,
    )


def _task_draft(
    research_goal: str, article_search_queries: list[str]
) -> ResearchTaskDraft:
    return ResearchTaskDraft(
        research_goal=research_goal,
        article_search_queries=article_search_queries,
    )


def _response_invalid(
    defect: AgentResponseDefect = AgentResponseDefect.RESPONSE_NOT_JSON,
    *,
    repair_hint: str | None = "use the declared JSON object schema",
) -> AgentResponseInvalidError:
    return AgentResponseInvalidError(defect, repair_hint=repair_hint)


class _RecordingPlannerRuntimeScope:
    def __init__(
        self,
        factory: RecordingPlannerRuntimeScopeFactory,
        runtime: ScriptedAgentRuntime,
    ) -> None:
        self._factory = factory
        self._runtime = runtime

    async def __aenter__(self) -> ScriptedAgentRuntime:
        if self._factory.enter_error is not None:
            raise self._factory.enter_error
        self._factory.entered.append(self._runtime)
        return self._runtime

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self._factory.exits.append((self._runtime, exc_type, exc, traceback))
        if self._factory.exit_observer is not None:
            self._factory.exit_observer()
        if self._factory.exit_error is not None:
            raise self._factory.exit_error
        return False


class RecordingPlannerRuntimeScopeFactory:
    def __init__(
        self,
        runtimes: Sequence[ScriptedAgentRuntime],
        *,
        enter_error: BaseException | None = None,
        exit_error: BaseException | None = None,
        exit_observer: Callable[[], None] | None = None,
    ) -> None:
        self._runtimes = list(runtimes)
        self.enter_error = enter_error
        self.exit_error = exit_error
        self.exit_observer = exit_observer
        self.created: list[ScriptedAgentRuntime] = []
        self.entered: list[ScriptedAgentRuntime] = []
        self.exits: list[
            tuple[
                ScriptedAgentRuntime,
                type[BaseException] | None,
                BaseException | None,
                TracebackType | None,
            ]
        ] = []

    def __call__(self) -> _RecordingPlannerRuntimeScope:
        runtime = self._runtimes[len(self.created)]
        self.created.append(runtime)
        return _RecordingPlannerRuntimeScope(self, runtime)


def _service(
    runtime: ScriptedAgentRuntime,
    *,
    exit_observer: Callable[[], None] | None = None,
    recorder: RecordingPlanningRecorder | None = None,
) -> tuple[QuestionPlanningService, RecordingPlannerRuntimeScopeFactory]:
    factory = RecordingPlannerRuntimeScopeFactory(
        [runtime],
        exit_observer=exit_observer,
    )
    if recorder is None:
        service = QuestionPlanningService(
            agent=QUESTION_PLANNER_AGENT,
            runtime_scope_factory=factory,
        )
    else:
        service = QuestionPlanningService(
            agent=QUESTION_PLANNER_AGENT,
            runtime_scope_factory=factory,
            recorder=recorder,
        )
    return (service, factory)


def _metric_attributes(
    metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    metric = next(
        (item for item in metrics if item["name"] == _PLANNER_OUTCOME_METRIC),
        None,
    )
    if metric is None:
        return []
    return [
        data_point.get("attributes", {}) for data_point in metric["data"]["data_points"]
    ]


def _assert_recorded(
    recorder: RecordingPlanningRecorder,
    *,
    outcome: PlanningOutcome | None,
) -> None:
    assert len(recorder.records) == 1
    recorded = recorder.records[0]
    assert recorded.agent_name == QUESTION_PLANNER_AGENT.name
    assert recorded.outcomes == ([] if outcome is None else [outcome])


@pytest.mark.parametrize(
    ("draft", "expected_plan_type"),
    [
        pytest.param(
            lambda: _draft(plan_type="direct_answer"),
            "direct_answer",
            id="direct",
        ),
        pytest.param(
            lambda: _draft(
                plan_type="search",
                research_tasks=[
                    _task_draft("NVIDIA の直近発表を確認する", ["NVIDIA AI GPU"])
                ],
            ),
            "search",
            id="search",
        ),
    ],
)
async def test_planner_returns_each_completed_two_plan_variant_after_scope_exit(
    draft: Callable[[], Any],
    expected_plan_type: str,
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime([draft()])
    metrics_at_scope_exit: list[dict[str, Any]] = []
    service, factory = _service(
        runtime,
        exit_observer=lambda: metrics_at_scope_exit.extend(
            _metric_attributes(collected_metrics(capfire))
        ),
    )

    plan = await service.plan(_input())

    assert plan.plan_type == expected_plan_type
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert (
        call.agent is QUESTION_PLANNER_AGENT,
        call.attempt_number,
        call.input.request.question,
        call.input.repair_context,
    ) == (True, 1, "今日のNVIDIAの発表は？", None)
    assert (factory.created, factory.entered, len(factory.exits)) == (
        [runtime],
        [runtime],
        1,
    )
    assert metrics_at_scope_exit == []
    assert _metric_attributes(collected_metrics(capfire)) == [
        {
            "result": "succeeded",
            "attempt_count": 1,
            "plan_type": expected_plan_type,
            "failure_code": "none",
        }
    ]


async def test_successful_plan_records_succeeded_outcome() -> None:
    """完成 plan は recorder へ成功結論を1回渡す。"""

    runtime = ScriptedAgentRuntime([_draft(plan_type="direct_answer")])
    recorder = RecordingPlanningRecorder()
    service, _factory = _service(runtime, recorder=recorder)

    plan = await service.plan(_input())

    assert plan.plan_type == "direct_answer"
    _assert_recorded(
        recorder,
        outcome=PlanningSucceeded(plan_type="direct_answer", attempt_count=1),
    )


async def test_semantic_response_defect_retries_once_without_leaking_question() -> None:
    question_sentinel = "RAW_QUESTION_MUST_NOT_REACH_REPAIR_OR_METRIC_8ab4"
    invalid = _draft(
        plan_type="direct_answer",
        research_tasks=[_task_draft("goal", [question_sentinel])],
    )
    runtime = ScriptedAgentRuntime(
        [
            invalid,
            _draft(
                plan_type="search",
                research_tasks=[
                    _task_draft("NVIDIA の直近発表を確認する", ["NVIDIA AI GPU"])
                ],
            ),
        ]
    )
    service, _factory = _service(runtime)

    plan = await service.plan(_input(question_sentinel))

    assert plan.plan_type == "search"
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert question_sentinel not in (runtime.calls[1].input.repair_context or "")


async def test_two_response_defects_propagate_second_error_and_record_not_created(
    capfire: CaptureLogfire,
) -> None:
    first_error = _response_invalid(AgentResponseDefect.RESPONSE_NOT_OBJECT)
    terminal_error = _response_invalid(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    runtime = ScriptedAgentRuntime([first_error, terminal_error])
    service, factory = _service(runtime)

    with pytest.raises(AgentResponseInvalidError) as raised:
        await service.plan(_input())

    assert raised.value is terminal_error
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert factory.exits[0][2] is terminal_error
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "failed") == 1
    assert _metric_attributes(metrics) == [
        {
            "result": "failed",
            "attempt_count": 2,
            "plan_type": "not_created",
            "failure_code": AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value,
        }
    ]


async def test_terminal_response_defect_records_failed_outcome() -> None:
    """分類済み終端は recorder へ failed を1回渡す。"""

    first_error = _response_invalid(AgentResponseDefect.RESPONSE_NOT_OBJECT)
    terminal_error = _response_invalid(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    runtime = ScriptedAgentRuntime([first_error, terminal_error])
    recorder = RecordingPlanningRecorder()
    service, _factory = _service(runtime, recorder=recorder)

    with pytest.raises(AgentResponseInvalidError):
        await service.plan(_input())

    _assert_recorded(
        recorder,
        outcome=PlanningFailed(
            failure_code=AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value,
            attempt_count=2,
        ),
    )


async def test_classified_provider_failure_does_not_retry_and_records_not_created(
    capfire: CaptureLogfire,
) -> None:
    error = AIProviderNetworkError()
    runtime = ScriptedAgentRuntime([error])
    service, factory = _service(runtime)

    with pytest.raises(AIProviderNetworkError) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert factory.exits[0][2] is error
    assert _metric_attributes(collected_metrics(capfire)) == [
        {
            "result": "failed",
            "attempt_count": 1,
            "plan_type": "not_created",
            "failure_code": "ai_error_network",
        }
    ]


async def test_classified_provider_failure_records_failed_outcome() -> None:
    """分類済み provider 失敗は recorder へ failed を渡す。"""

    error = AIProviderNetworkError()
    runtime = ScriptedAgentRuntime([error])
    recorder = RecordingPlanningRecorder()
    service, _factory = _service(runtime, recorder=recorder)

    with pytest.raises(AIProviderNetworkError):
        await service.plan(_input())

    _assert_recorded(
        recorder,
        outcome=PlanningFailed(
            failure_code="ai_error_network",
            attempt_count=1,
        ),
    )


async def test_bare_provider_error_propagates_as_unclassified_without_outcome_metric(
    capfire: CaptureLogfire,
) -> None:
    error = AIProviderError()
    runtime = ScriptedAgentRuntime([error])
    service, factory = _service(runtime)

    with pytest.raises(AIProviderError) as raised:
        await service.plan(_input())

    phase = next(
        span
        for span in capfire.exporter.exported_spans
        if span.name == "agent_phase"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    )
    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert factory.exits[0][2] is error
    assert _metric_attributes(collected_metrics(capfire)) == []
    assert phase.status.status_code is StatusCode.ERROR


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("unknown planner failure"), id="unknown"),
        pytest.param(asyncio.CancelledError(), id="cancellation"),
    ],
)
async def test_unclassified_failure_and_cancellation_propagate_without_metric(
    error: BaseException,
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime([error])
    recorder = RecordingPlanningRecorder()
    service, _factory = _service(runtime, recorder=recorder)

    with pytest.raises(type(error)) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert _metric_attributes(collected_metrics(capfire)) == []
    _assert_recorded(recorder, outcome=None)


@pytest.mark.parametrize("failure_point", ["enter", "exit"])
async def test_scope_failure_propagates_without_plan_or_metric(
    failure_point: str,
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError(f"runtime scope {failure_point} failed")
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                plan_type="search",
                research_tasks=[_task_draft("NVIDIA の根拠を確認する", ["NVIDIA"])],
            )
        ]
    )
    factory = RecordingPlannerRuntimeScopeFactory(
        [runtime],
        enter_error=error if failure_point == "enter" else None,
        exit_error=error if failure_point == "exit" else None,
    )
    recorder = RecordingPlanningRecorder()
    service = QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=factory,
        recorder=recorder,
    )

    with pytest.raises(RuntimeError) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert len(runtime.calls) == (0 if failure_point == "enter" else 1)
    assert _metric_attributes(collected_metrics(capfire)) == []
    _assert_recorded(recorder, outcome=None)


async def test_search_plan_preserves_typed_time_window_through_service() -> None:
    target_time_window = TargetTimeWindow.model_validate(
        {"kind": "last_n_days", "days": 7}
    )
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                plan_type="search",
                research_tasks=[_task_draft("NVIDIA の直近発表を確認する", ["NVIDIA"])],
                target_time_window=target_time_window,
            )
        ]
    )
    service, _factory = _service(runtime)

    plan = await service.plan(_input())

    assert plan.target_time_window == target_time_window


@pytest.mark.parametrize("defect", list(AgentResponseDefect))
async def test_each_response_defect_retries_once_in_the_same_runtime(
    defect: AgentResponseDefect,
) -> None:
    first_error = _response_invalid(defect)
    runtime = ScriptedAgentRuntime(
        [
            first_error,
            _draft(
                plan_type="search",
                research_tasks=[_task_draft("NVIDIA の発表根拠を確認する", ["NVIDIA"])],
            ),
        ]
    )
    service, factory = _service(runtime)

    plan = await service.plan(_input())

    assert plan.plan_type == "search"
    assert (factory.created, factory.entered) == ([runtime], [runtime])
    assert len(factory.exits) == 1
    assert [call.agent for call in runtime.calls] == [
        QUESTION_PLANNER_AGENT,
        QUESTION_PLANNER_AGENT,
    ]
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert [call.input.repair_context for call in runtime.calls] == [
        None,
        str(first_error),
    ]


async def test_close_error_replaces_terminal_response_defect_without_metric(
    capfire: CaptureLogfire,
) -> None:
    first_error = _response_invalid(AgentResponseDefect.RESPONSE_NOT_OBJECT)
    terminal_error = _response_invalid(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    close_error = AIProviderNetworkError()
    runtime = ScriptedAgentRuntime([first_error, terminal_error])
    factory = RecordingPlannerRuntimeScopeFactory(
        [runtime],
        exit_error=close_error,
    )
    recorder = RecordingPlanningRecorder()
    service = QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=factory,
        recorder=recorder,
    )

    with pytest.raises(AIProviderNetworkError) as raised:
        await service.plan(_input())

    assert raised.value is close_error
    assert close_error.__context__ is terminal_error
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert (
        factory.exits[0][0] is runtime,
        factory.exits[0][1] is type(terminal_error),
        factory.exits[0][2] is terminal_error,
        factory.exits[0][3] is not None,
    ) == (True, True, True, True)
    assert _metric_attributes(collected_metrics(capfire)) == []
    _assert_recorded(recorder, outcome=None)


@pytest.mark.parametrize(
    ("error", "expected_failure_code"),
    [
        pytest.param(
            AIProviderNetworkError("RAW_PROVIDER_MESSAGE_MUST_NOT_ENTER_METRICS_26e9"),
            "ai_error_network",
            id="provider-error",
        ),
        pytest.param(
            AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
            "ai_error_output_blocked",
            id="blocked-output",
        ),
    ],
)
async def test_classified_non_response_error_propagates_without_retry(
    error: BaseException,
    expected_failure_code: str,
    capfire: CaptureLogfire,
) -> None:
    question_sentinel = "PLANNER_QUESTION_MUST_NOT_ENTER_METRICS_7f0a"
    runtime = ScriptedAgentRuntime([error])
    metrics_at_scope_exit: list[dict[str, Any]] = []
    service, factory = _service(
        runtime,
        exit_observer=lambda: metrics_at_scope_exit.extend(
            _metric_attributes(collected_metrics(capfire))
        ),
    )

    with pytest.raises(type(error)) as raised:
        await service.plan(_input(question_sentinel))

    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert len(factory.exits) == 1
    assert metrics_at_scope_exit == []
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "failed") == 1
    assert _metric_attributes(metrics) == [
        {
            "result": "failed",
            "attempt_count": 1,
            "plan_type": "not_created",
            "failure_code": expected_failure_code,
        }
    ]
    metric_dump = json.dumps(metrics, default=str, ensure_ascii=False)
    assert question_sentinel not in metric_dump
    assert "RAW_PROVIDER_MESSAGE_MUST_NOT_ENTER_METRICS_26e9" not in metric_dump


@pytest.mark.parametrize(
    ("error", "exception_message"),
    [
        pytest.param(
            TimeoutError("RAW_EXCEPTION_MESSAGE_MUST_NOT_ENTER_METRICS_79a3"),
            "RAW_EXCEPTION_MESSAGE_MUST_NOT_ENTER_METRICS_79a3",
            id="unknown-error",
        ),
        pytest.param(asyncio.CancelledError(), None, id="cancellation"),
    ],
)
async def test_unknown_error_and_cancellation_propagate_by_identity(
    error: BaseException,
    exception_message: str | None,
    capfire: CaptureLogfire,
) -> None:
    question_sentinel = "PLANNER_QUESTION_MUST_NOT_ENTER_METRICS_58d2"
    runtime = ScriptedAgentRuntime([error])
    recorder = RecordingPlanningRecorder()
    service, factory = _service(runtime, recorder=recorder)

    with pytest.raises(type(error)) as raised:
        await service.plan(_input(question_sentinel))

    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert len(factory.exits) == 1
    assert factory.exits[0][2] is error
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics) == []
    metric_dump = json.dumps(metrics, default=str, ensure_ascii=False)
    assert question_sentinel not in metric_dump
    if exception_message is not None:
        assert exception_message not in metric_dump
    _assert_recorded(recorder, outcome=None)


@pytest.mark.parametrize(
    ("failure_point", "runtime_call_count", "error"),
    [
        pytest.param(
            "enter",
            0,
            RuntimeError("planner runtime scope enter failed"),
            id="runtime-enter",
        ),
        pytest.param(
            "exit",
            1,
            RuntimeError("planner runtime scope exit failed"),
            id="runtime-exit",
        ),
        pytest.param("enter", 0, AIProviderNetworkError(), id="classified-enter"),
        pytest.param("exit", 1, AIProviderNetworkError(), id="classified-exit"),
    ],
)
async def test_all_runtime_scope_failures_propagate_without_plan_or_metric(
    failure_point: str,
    runtime_call_count: int,
    error: BaseException,
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                plan_type="search",
                research_tasks=[
                    _task_draft("must not be returned", ["must not be returned"])
                ],
            )
        ]
    )
    factory = RecordingPlannerRuntimeScopeFactory(
        [runtime],
        enter_error=error if failure_point == "enter" else None,
        exit_error=error if failure_point == "exit" else None,
    )
    recorder = RecordingPlanningRecorder()
    service = QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=factory,
        recorder=recorder,
    )

    with pytest.raises(type(error)) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert len(runtime.calls) == runtime_call_count
    assert _metric_attributes(collected_metrics(capfire)) == []
    _assert_recorded(recorder, outcome=None)


async def test_two_plan_calls_activate_fresh_runtime_scopes() -> None:
    first_runtime = ScriptedAgentRuntime([_draft(plan_type="direct_answer")])
    second_runtime = ScriptedAgentRuntime(
        [
            _draft(
                plan_type="search",
                research_tasks=[_task_draft("second goal", ["second query"])],
            )
        ]
    )
    factory = RecordingPlannerRuntimeScopeFactory([first_runtime, second_runtime])
    service = QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=factory,
    )

    first = await service.plan(_input("first"))
    second = await service.plan(_input("second"))

    assert (first.plan_type, second.plan_type) == ("direct_answer", "search")
    assert factory.created == [first_runtime, second_runtime]
    assert factory.entered == [first_runtime, second_runtime]
    assert len(factory.exits) == 2
    assert first_runtime is not second_runtime


async def test_retry_success_metric_records_after_scope_exit_without_repair_detail(
    capfire: CaptureLogfire,
) -> None:
    invalid = _response_invalid(
        AgentResponseDefect.RESPONSE_NOT_JSON,
        repair_hint="REPAIR_HINT_MUST_NOT_ENTER_METRICS_4d7f",
    )
    runtime = ScriptedAgentRuntime(
        [
            invalid,
            _draft(
                plan_type="search",
                research_tasks=[
                    _task_draft("NVIDIA の根拠を確認する", ["NVIDIA AI GPU"])
                ],
            ),
        ]
    )
    metrics_at_scope_exit: list[dict[str, Any]] = []
    service, _factory = _service(
        runtime,
        exit_observer=lambda: metrics_at_scope_exit.extend(
            _metric_attributes(collected_metrics(capfire))
        ),
    )

    plan = await service.plan(_input())

    assert plan.plan_type == "search"
    assert metrics_at_scope_exit == []
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "succeeded") == 1
    assert _metric_attributes(metrics) == [
        {
            "result": "succeeded",
            "attempt_count": 2,
            "plan_type": "search",
            "failure_code": "none",
        }
    ]
    assert "REPAIR_HINT_MUST_NOT_ENTER_METRICS_4d7f" not in json.dumps(
        metrics, default=str, ensure_ascii=False
    )


async def test_retry_success_records_attempt_count() -> None:
    """retry 成功は recorder へ実際の試行回数を渡す。"""

    invalid = _response_invalid(AgentResponseDefect.RESPONSE_NOT_JSON)
    runtime = ScriptedAgentRuntime(
        [
            invalid,
            _draft(
                plan_type="search",
                research_tasks=[
                    _task_draft("NVIDIA の根拠を確認する", ["NVIDIA AI GPU"])
                ],
            ),
        ]
    )
    recorder = RecordingPlanningRecorder()
    service, _factory = _service(runtime, recorder=recorder)

    plan = await service.plan(_input())

    assert plan.plan_type == "search"
    _assert_recorded(
        recorder,
        outcome=PlanningSucceeded(plan_type="search", attempt_count=2),
    )


@pytest.mark.parametrize(
    ("outcomes", "attempt_count", "plan_type"),
    [
        pytest.param(
            lambda: [_draft(plan_type="direct_answer")],
            1,
            "direct_answer",
            id="direct",
        ),
        pytest.param(
            lambda: [
                _response_invalid(),
                _draft(
                    plan_type="search",
                    research_tasks=[
                        _task_draft("NVIDIA の外部根拠を確認する", ["NVIDIA"])
                    ],
                ),
            ],
            2,
            "search",
            id="retry-search",
        ),
    ],
)
async def test_outcome_metric_records_once_without_model_visible_text(
    outcomes: Callable[[], list[Any]],
    attempt_count: int,
    plan_type: str,
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime(outcomes())
    service, _factory = _service(runtime)

    await service.plan(_input("MODEL_VISIBLE_QUESTION_SENTINEL_86ba"))

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "succeeded") == 1
    assert _metric_attributes(metrics) == [
        {
            "result": "succeeded",
            "attempt_count": attempt_count,
            "plan_type": plan_type,
            "failure_code": "none",
        }
    ]
    assert "MODEL_VISIBLE_QUESTION_SENTINEL_86ba" not in json.dumps(
        metrics, default=str, ensure_ascii=False
    )
