"""QuestionPlanningService の2 plan retry・failure・metric contract。"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.planning.service import QuestionPlanningService
from app.agent.question_context.contract import QuestionContext
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import (
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_PLANNER_OUTCOME_METRIC = "vector.agent.planner.outcome"


def _contracts() -> Any:
    return importlib.import_module("app.agent.planning.contract")


def _required_contract(name: str) -> Any:
    value = getattr(_contracts(), name, None)
    if value is None:
        pytest.fail(f"planning contract must define {name}")
    return value


def _input(question: str = "今日のNVIDIAの発表は？") -> Any:
    return _required_contract("PlanningRequest")(
        context=QuestionContext(standalone_question=question),
        as_of=datetime(2026, 7, 20, tzinfo=UTC),
    )


def _draft(
    *,
    plan_type: str,
    article_search_queries: list[str] | None = None,
    research_goals: list[str] | None = None,
    target_time_window: object | None = None,
) -> Any:
    return _required_contract("QuestionPlanDraft")(
        plan_type=plan_type,
        article_search_queries=article_search_queries or [],
        research_goals=research_goals or [],
        target_time_window=target_time_window,
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
) -> tuple[QuestionPlanningService, RecordingPlannerRuntimeScopeFactory]:
    factory = RecordingPlannerRuntimeScopeFactory(
        [runtime],
        exit_observer=exit_observer,
    )
    return (
        QuestionPlanningService(
            agent=QUESTION_PLANNER_AGENT,
            runtime_scope_factory=factory,
        ),
        factory,
    )


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
                article_search_queries=["NVIDIA AI GPU"],
                research_goals=["NVIDIA の直近発表を確認する"],
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
        call.input.request.context.standalone_question,
        call.input.previous_error,
    ) == (True, 1, "今日のNVIDIAの発表は？", None)
    assert (factory.created, factory.entered, len(factory.exits)) == (
        [runtime],
        [runtime],
        1,
    )
    assert metrics_at_scope_exit == []
    assert _metric_attributes(collected_metrics(capfire)) == [
        {
            "result": "planned",
            "retry_used": False,
            "plan_type": expected_plan_type,
            "failure_code": "none",
        }
    ]


async def test_semantic_response_defect_retries_once_without_leaking_question() -> None:
    question_sentinel = "RAW_QUESTION_MUST_NOT_REACH_REPAIR_OR_METRIC_8ab4"
    invalid = _draft(
        plan_type="direct_answer",
        article_search_queries=[question_sentinel],
    )
    runtime = ScriptedAgentRuntime(
        [
            invalid,
            _draft(
                plan_type="search",
                article_search_queries=["NVIDIA AI GPU"],
                research_goals=["NVIDIA の直近発表を確認する"],
            ),
        ]
    )
    service, _factory = _service(runtime)

    plan = await service.plan(_input(question_sentinel))

    assert plan.plan_type == "search"
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert question_sentinel not in (runtime.calls[1].input.previous_error or "")


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
            "retry_used": True,
            "plan_type": "not_created",
            "failure_code": AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value,
        }
    ]


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
            "retry_used": False,
            "plan_type": "not_created",
            "failure_code": "ai_error_network",
        }
    ]


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
    service, _factory = _service(runtime)

    with pytest.raises(type(error)) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert _metric_attributes(collected_metrics(capfire)) == []


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
                article_search_queries=["NVIDIA"],
                research_goals=["NVIDIA の根拠を確認する"],
            )
        ]
    )
    factory = RecordingPlannerRuntimeScopeFactory(
        [runtime],
        enter_error=error if failure_point == "enter" else None,
        exit_error=error if failure_point == "exit" else None,
    )
    service = QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=factory,
    )

    with pytest.raises(RuntimeError) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert len(runtime.calls) == (0 if failure_point == "enter" else 1)
    assert _metric_attributes(collected_metrics(capfire)) == []


async def test_search_plan_preserves_typed_time_window_through_service() -> None:
    target_time_window = _required_contract("TargetTimeWindow").model_validate(
        {"kind": "last_n_days", "days": 7}
    )
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                plan_type="search",
                article_search_queries=["NVIDIA"],
                research_goals=["NVIDIA の直近発表を確認する"],
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
                article_search_queries=["NVIDIA"],
                research_goals=["NVIDIA の発表根拠を確認する"],
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
    assert [call.input.previous_error for call in runtime.calls] == [
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
    service = QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=factory,
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
            "retry_used": False,
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
    service, factory = _service(runtime)

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
                article_search_queries=["must not be returned"],
                research_goals=["must not be returned"],
            )
        ]
    )
    factory = RecordingPlannerRuntimeScopeFactory(
        [runtime],
        enter_error=error if failure_point == "enter" else None,
        exit_error=error if failure_point == "exit" else None,
    )
    service = QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=factory,
    )

    with pytest.raises(type(error)) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert len(runtime.calls) == runtime_call_count
    assert _metric_attributes(collected_metrics(capfire)) == []


async def test_two_plan_calls_activate_fresh_runtime_scopes() -> None:
    first_runtime = ScriptedAgentRuntime([_draft(plan_type="direct_answer")])
    second_runtime = ScriptedAgentRuntime(
        [
            _draft(
                plan_type="search",
                article_search_queries=["second query"],
                research_goals=["second goal"],
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
                article_search_queries=["NVIDIA AI GPU"],
                research_goals=["NVIDIA の根拠を確認する"],
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
    assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "planned") == 1
    assert _metric_attributes(metrics) == [
        {
            "result": "planned",
            "retry_used": True,
            "plan_type": "search",
            "failure_code": "none",
        }
    ]
    assert "REPAIR_HINT_MUST_NOT_ENTER_METRICS_4d7f" not in json.dumps(
        metrics, default=str, ensure_ascii=False
    )


@pytest.mark.parametrize(
    ("outcomes", "retry_used", "plan_type"),
    [
        pytest.param(
            lambda: [_draft(plan_type="direct_answer")],
            False,
            "direct_answer",
            id="direct",
        ),
        pytest.param(
            lambda: [
                _response_invalid(),
                _draft(
                    plan_type="search",
                    article_search_queries=["NVIDIA"],
                    research_goals=["NVIDIA の外部根拠を確認する"],
                ),
            ],
            True,
            "search",
            id="retry-search",
        ),
    ],
)
async def test_outcome_metric_records_once_without_model_visible_text(
    outcomes: Callable[[], list[Any]],
    retry_used: bool,
    plan_type: str,
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime(outcomes())
    service, _factory = _service(runtime)

    await service.plan(_input("MODEL_VISIBLE_QUESTION_SENTINEL_86ba"))

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "planned") == 1
    assert _metric_attributes(metrics) == [
        {
            "result": "planned",
            "retry_used": retry_used,
            "plan_type": plan_type,
            "failure_code": "none",
        }
    ]
    assert "MODEL_VISIBLE_QUESTION_SENTINEL_86ba" not in json.dumps(
        metrics, default=str, ensure_ascii=False
    )
