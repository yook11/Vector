"""Question planning service policy and tracing tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import logfire
import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

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
from app.logfire.redaction import install_exception_redaction
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
    spans_named,
)

_PLANNER_OUTCOME_METRIC = "vector.agent.planner.outcome"
_PHASE_SPAN_NAME = "agent_phase"
_PROVIDER_SPAN_NAME = "agent_provider_call"


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
    reason: str = "test reason",
) -> QuestionPlanDraft:
    return QuestionPlanDraft(
        retrieval_mode=mode,
        internal_queries=internal_queries or [],
        external_collection_goals=external_collection_goals or [],
        reason=reason,
    )


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


@dataclass(frozen=True, slots=True)
class RuntimeInvokeCall:
    agent: Agent[Any, Any]
    input: PlanningAttemptInput
    attempt_number: int


class FakeRuntime:
    """Runtime fake that owns one attempt at a time and no policy."""

    def __init__(
        self,
        outcomes: Sequence[QuestionPlanDraft | BaseException],
        *,
        trace_attempts: bool = False,
    ) -> None:
        self._outcomes = list(outcomes)
        self._trace_attempts = trace_attempts
        self.calls: list[RuntimeInvokeCall] = []

    async def invoke[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> OutputT:
        assert isinstance(input, PlanningAttemptInput)
        self.calls.append(
            RuntimeInvokeCall(
                agent=agent,
                input=input,
                attempt_number=attempt_number,
            )
        )
        outcome = self._outcomes.pop(0)
        if not self._trace_attempts:
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome  # type: ignore[return-value]

        deferred_error: BaseException | None = None
        with logfire.span(
            _PROVIDER_SPAN_NAME,
            agent_name=agent.name,
            attempt_number=attempt_number,
        ) as span:
            if isinstance(outcome, AgentResponseInvalidError):
                span.set_attribute("result", "invalid_response")
                span.set_attribute("error.type", outcome.defect.value)
                span.set_status(StatusCode.ERROR)
                deferred_error = outcome
            elif isinstance(
                outcome,
                (AIProviderNetworkError, AIProviderOutputBlockedError),
            ):
                span.set_attribute("result", "provider_error")
                span.set_attribute("error.type", outcome.CODE)
                span.set_status(StatusCode.ERROR)
                deferred_error = outcome
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                span.set_attribute("result", "succeeded")
                span.set_attribute("gen_ai.usage.input_tokens", 11)
        if deferred_error is not None:
            raise deferred_error
        return outcome  # type: ignore[return-value]


class _RuntimeScope:
    def __init__(self, factory: FakeRuntimeScopeFactory, runtime: FakeRuntime) -> None:
        self._factory = factory
        self._runtime = runtime

    async def __aenter__(self) -> FakeRuntime:
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


class FakeRuntimeScopeFactory:
    def __init__(self, runtimes: Sequence[FakeRuntime]) -> None:
        self._runtimes = list(runtimes)
        self.created: list[FakeRuntime] = []
        self.entered: list[FakeRuntime] = []
        self.exits: list[
            tuple[
                FakeRuntime,
                type[BaseException] | None,
                BaseException | None,
                TracebackType | None,
            ]
        ] = []

    def __call__(self) -> _RuntimeScope:
        runtime = self._runtimes[len(self.created)]
        self.created.append(runtime)
        return _RuntimeScope(self, runtime)


def _service(
    runtime: FakeRuntime,
    *,
    agent: Agent[PlanningAttemptInput, QuestionPlanDraft] = QUESTION_PLANNER_AGENT,
) -> tuple[QuestionPlanningService, FakeRuntimeScopeFactory]:
    factory = FakeRuntimeScopeFactory([runtime])
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
    runtime = FakeRuntime(
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


@pytest.mark.parametrize("defect", list(AgentResponseDefect))
async def test_each_response_defect_retries_once_in_the_same_runtime(
    defect: AgentResponseDefect,
) -> None:
    first_error = _response_invalid(defect)
    runtime = FakeRuntime(
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
    runtime = FakeRuntime([first_error, second_error])
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
    runtime = FakeRuntime([error])
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
    runtime = FakeRuntime([error])
    service, factory = _service(runtime)

    with pytest.raises(type(error)) as raised:
        await service.plan(_input())

    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert len(factory.exits) == 1
    assert factory.exits[0][2] is error
    assert _metric_attributes(collected_metrics(capfire), _PLANNER_OUTCOME_METRIC) == []


async def test_two_plan_calls_activate_fresh_runtime_scopes() -> None:
    first_runtime = FakeRuntime([_draft("none", reason="first")])
    second_runtime = FakeRuntime(
        [_draft("internal", internal_queries=["second query"], reason="second")]
    )
    factory = FakeRuntimeScopeFactory([first_runtime, second_runtime])
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
    runtime = FakeRuntime(
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
    runtime = FakeRuntime(
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
    runtime = FakeRuntime(outcomes)
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


async def test_success_has_one_phase_with_one_provider_child(
    capfire: CaptureLogfire,
) -> None:
    runtime = FakeRuntime(
        [_draft("internal", internal_queries=["NVIDIA"])],
        trace_attempts=True,
    )
    service, _factory = _service(runtime)

    await service.plan(_input())

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    provider = one_span_named(capfire, _PROVIDER_SPAN_NAME)
    assert domain_attr_keys(phase["attributes"]) == {"phase", "agent_name"}
    assert (
        phase["attributes"]["phase"],
        phase["attributes"]["agent_name"],
        provider["parent"]["span_id"],
        provider["context"]["trace_id"],
    ) == (
        "question_planning",
        QUESTION_PLANNER_AGENT.name,
        phase["context"]["span_id"],
        phase["context"]["trace_id"],
    )
    assert not any(key.startswith("gen_ai.usage.") for key in phase["attributes"])


async def test_retry_keeps_two_provider_attempts_under_one_phase_without_error_event(
    capfire: CaptureLogfire,
) -> None:
    runtime = FakeRuntime(
        [
            _response_invalid(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
            _draft("none"),
        ],
        trace_attempts=True,
    )
    service, _factory = _service(runtime)

    await service.plan(_input())

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    assert len(providers) == 2
    assert [span["attributes"]["attempt_number"] for span in providers] == [1, 2]
    assert all(
        span["parent"]["span_id"] == phase["context"]["span_id"] for span in providers
    )
    assert exception_event(phase) is None
    assert phase.get("status", {}).get("description") in (None, "")


async def test_unknown_error_keeps_redacted_phase_exception_without_sensitive_attrs(
    capfire: CaptureLogfire,
) -> None:
    install_exception_redaction()
    sentinel = "UNCLASSIFIED_PLANNER_SENTINEL_1f78"
    error = RuntimeError(sentinel)
    runtime = FakeRuntime([error], trace_attempts=True)
    service, _factory = _service(runtime)

    with pytest.raises(RuntimeError) as raised:
        await service.plan(_input("QUESTION_SENTINEL_ef34"))

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    raw_phases = [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _PHASE_SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    event = exception_event(phase)
    assert raised.value is error
    assert len(raw_phases) == 1
    assert event is not None
    assert event["attributes"]["exception.message"] == "[redacted]"
    assert event["attributes"]["exception.stacktrace"] == "[redacted]"
    assert raw_phases[0].status.status_code is StatusCode.ERROR
    assert raw_phases[0].status.description == "[redacted]"
    assert domain_attr_keys(phase["attributes"]) == {"phase", "agent_name"}
    span_dump = json.dumps(phase, default=str, ensure_ascii=False)
    for unsafe in (
        sentinel,
        "QUESTION_SENTINEL_ef34",
        QUESTION_PLANNER_AGENT.prompt.version,
        "run_id",
    ):
        assert unsafe not in span_dump
