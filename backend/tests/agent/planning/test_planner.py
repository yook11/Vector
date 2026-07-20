"""QuestionPlanningService の retry・fallback・metrics policy を検証する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, cast

import pytest
from logfire.testing import CaptureLogfire

from app.agent.agent import Agent
from app.agent.contract import RetrievalMode
from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.planning.contract import (
    ExternalResearchTask,
    PlanningAttemptInput,
    PlanningRequest,
    QuestionPlanDraft,
    safe_fallback_plan,
)
from app.agent.planning.service import QuestionPlanningService
from app.agent.question_context.contract import QuestionContext
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.analysis.ai_provider_errors import (
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_PLANNER_OUTCOME_METRIC = "vector.agent.planner.outcome"


def _input(question: str = "今日のNVIDIAの発表は？") -> PlanningRequest:
    return PlanningRequest(
        context=QuestionContext(standalone_question=question),
        as_of=datetime(2026, 6, 29, tzinfo=UTC),
    )


def _draft(
    mode: RetrievalMode,
    *,
    internal_queries: list[str] | None = None,
    external_collection_goals: list[str] | None = None,
    target_time_window: object | None = None,
    reason: str = "test reason",
) -> QuestionPlanDraft:
    return QuestionPlanDraft(
        retrieval_mode=mode,
        internal_queries=internal_queries or [],
        external_collection_goals=external_collection_goals or [],
        target_time_window=target_time_window,
        reason=reason,
    )


def _target_time_window(**payload: object) -> object:
    target_time_window_type = getattr(
        __import__("app.agent.planning.contract", fromlist=["TargetTimeWindow"]),
        "TargetTimeWindow",
        None,
    )
    if target_time_window_type is None:
        pytest.fail("app.agent.planning.contract must define TargetTimeWindow")
    return cast(type[object], target_time_window_type).model_validate(payload)


def _external_task(
    collection_goal: str = "外部根拠を確認する",
) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=collection_goal)


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
        self._factory.entered.append(self._runtime)
        return self._runtime

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self._factory.exits.append((self._runtime, exc_type, exc, traceback))
        return False


class RecordingPlannerRuntimeScopeFactory:
    def __init__(self, runtimes: Sequence[ScriptedAgentRuntime]) -> None:
        self._runtimes = list(runtimes)
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
    agent: Agent[PlanningAttemptInput, QuestionPlanDraft] = QUESTION_PLANNER_AGENT,
) -> tuple[QuestionPlanningService, RecordingPlannerRuntimeScopeFactory]:
    factory = RecordingPlannerRuntimeScopeFactory([runtime])
    return (
        QuestionPlanningService(
            agent=agent,
            runtime_scope_factory=factory,
        ),
        factory,
    )


def _metric_attributes(
    metrics: list[dict[str, Any]],
    metric_name: str,
) -> list[dict[str, Any]]:
    metric = next((item for item in metrics if item["name"] == metric_name), None)
    if metric is None:
        return []
    return [
        data_point.get("attributes", {}) for data_point in metric["data"]["data_points"]
    ]


async def test_plan_activates_one_scope_and_invokes_declared_agent_once() -> None:
    request = _input()
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                "external",
                external_collection_goals=["  NVIDIA の直近発表を確認する  "],
            )
        ]
    )
    service, factory = _service(runtime)

    plan = await service.plan(request)

    assert plan.external_research_tasks == [
        _external_task("NVIDIA の直近発表を確認する")
    ]
    assert (factory.created, factory.entered) == ([runtime], [runtime])
    assert len(factory.exits) == 1
    assert factory.exits[0][0] is runtime
    assert factory.exits[0][1:3] == (None, None)
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert (
        call.agent is QUESTION_PLANNER_AGENT,
        call.attempt_number,
        call.input.request is request,
        call.input.previous_error,
    ) == (True, 1, True, None)


@pytest.mark.parametrize("mode", ["external", "internal_and_external"])
async def test_planner_service_keeps_typed_time_window_on_completed_external_plan(
    mode: RetrievalMode,
) -> None:
    target_time_window = _target_time_window(kind="last_n_days", days=7)
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                mode,
                internal_queries=["NVIDIA"],
                external_collection_goals=["NVIDIA の直近発表を確認する"],
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
                "external",
                external_collection_goals=["NVIDIA の発表根拠を確認する"],
            ),
        ]
    )
    service, factory = _service(runtime)

    plan = await service.plan(_input())

    assert plan.retrieval_mode == "external"
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


async def test_two_response_defects_fall_back_after_exactly_two_attempts() -> None:
    request = _input("保存済みの記事からAI半導体ニュースをまとめて")
    first_error = _response_invalid(AgentResponseDefect.RESPONSE_NOT_OBJECT)
    second_error = _response_invalid(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    runtime = ScriptedAgentRuntime([first_error, second_error])
    service, factory = _service(runtime)

    plan = await service.plan(request)

    assert plan == safe_fallback_plan(
        fallback_query="保存済みの記事からAI半導体ニュースをまとめて"
    )
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert [call.input.previous_error for call in runtime.calls] == [
        None,
        str(first_error),
    ]
    assert (factory.created, factory.entered) == ([runtime], [runtime])
    assert len(factory.exits) == 1


@pytest.mark.parametrize(
    ("error", "expected_failure_code"),
    [
        pytest.param(AIProviderNetworkError(), "ai_error_network", id="provider-error"),
        pytest.param(
            AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
            "ai_error_output_blocked",
            id="blocked-output",
        ),
    ],
)
async def test_classified_non_response_error_falls_back_without_retry(
    error: BaseException,
    expected_failure_code: str,
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime([error])
    service, factory = _service(runtime)

    plan = await service.plan(_input("保存済み記事で見て"))

    assert plan == safe_fallback_plan(fallback_query="保存済み記事で見て")
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert len(factory.exits) == 1
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _PLANNER_OUTCOME_METRIC) == [
        {
            "result": "fallback",
            "retry_used": False,
            "planned_retrieval_mode": plan.retrieval_mode,
            "failure_code": expected_failure_code,
        }
    ]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(TimeoutError("provider timeout"), id="unknown-error"),
        pytest.param(asyncio.CancelledError(), id="cancellation"),
    ],
)
async def test_unknown_error_and_cancellation_propagate_by_identity(
    error: BaseException,
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime([error])
    service, factory = _service(runtime)

    with pytest.raises(type(error)) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert len(factory.exits) == 1
    assert factory.exits[0][2] is error
    assert _metric_attributes(collected_metrics(capfire), _PLANNER_OUTCOME_METRIC) == []


async def test_two_plan_calls_activate_fresh_runtime_scopes() -> None:
    first_runtime = ScriptedAgentRuntime([_draft("none", reason="first")])
    second_runtime = ScriptedAgentRuntime(
        [_draft("internal", internal_queries=["second query"], reason="second")]
    )
    factory = RecordingPlannerRuntimeScopeFactory([first_runtime, second_runtime])
    service = QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=factory,
    )

    first = await service.plan(_input("first"))
    second = await service.plan(_input("second"))

    assert (first.retrieval_mode, second.retrieval_mode) == ("none", "internal")
    assert factory.created == [first_runtime, second_runtime]
    assert factory.entered == [first_runtime, second_runtime]
    assert len(factory.exits) == 2
    assert first_runtime is not second_runtime


async def test_retry_success_metric_reports_no_failure_and_no_repair_detail(
    capfire: CaptureLogfire,
) -> None:
    invalid = _response_invalid(
        AgentResponseDefect.RESPONSE_NOT_JSON,
        repair_hint="REPAIR_HINT_MUST_NOT_ENTER_METRICS_4d7f",
    )
    runtime = ScriptedAgentRuntime(
        [invalid, _draft("internal", internal_queries=["NVIDIA AI GPU"])]
    )
    service, _factory = _service(runtime)

    plan = await service.plan(_input())

    assert plan.retrieval_mode == "internal"
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _PLANNER_OUTCOME_METRIC) == [
        {
            "result": "planned",
            "retry_used": True,
            "planned_retrieval_mode": "internal",
            "failure_code": "none",
        }
    ]
    assert "REPAIR_HINT_MUST_NOT_ENTER_METRICS_4d7f" not in json.dumps(
        metrics, default=str, ensure_ascii=False
    )


async def test_fallback_after_retry_failure_records_final_failure_code(
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime(
        [
            _response_invalid(AgentResponseDefect.RESPONSE_NOT_JSON),
            _response_invalid(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
        ]
    )
    service, _factory = _service(runtime)

    plan = await service.plan(_input())

    assert plan == safe_fallback_plan(
        fallback_query=_input().context.standalone_question
    )
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _PLANNER_OUTCOME_METRIC) == [
        {
            "result": "fallback",
            "retry_used": True,
            "planned_retrieval_mode": plan.retrieval_mode,
            "failure_code": AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value,
        }
    ]


@pytest.mark.parametrize(
    ("outcomes", "result", "retry_used", "mode", "failure_code"),
    [
        pytest.param(
            [_draft("internal", internal_queries=["NVIDIA"])],
            "planned",
            False,
            "internal",
            "none",
            id="planned",
        ),
        pytest.param(
            [
                _response_invalid(),
                _draft(
                    "external",
                    external_collection_goals=["NVIDIA の外部根拠を確認する"],
                ),
            ],
            "planned",
            True,
            "external",
            "none",
            id="retry-success",
        ),
        pytest.param(
            [AIProviderNetworkError()],
            "fallback",
            False,
            "internal",
            "ai_error_network",
            id="fallback",
        ),
    ],
)
async def test_outcome_metric_records_once_without_model_visible_text(
    outcomes: list[QuestionPlanDraft | BaseException],
    result: str,
    retry_used: bool,
    mode: RetrievalMode,
    failure_code: str,
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime(outcomes)
    service, _factory = _service(runtime)

    await service.plan(_input("MODEL_VISIBLE_QUESTION_SENTINEL_86ba"))

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, result) == 1
    assert _metric_attributes(metrics, _PLANNER_OUTCOME_METRIC) == [
        {
            "result": result,
            "retry_used": retry_used,
            "planned_retrieval_mode": mode,
            "failure_code": failure_code,
        }
    ]
    assert "MODEL_VISIBLE_QUESTION_SENTINEL_86ba" not in json.dumps(
        metrics, default=str, ensure_ascii=False
    )
