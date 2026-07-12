"""Question planning flow tests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from pydantic import ValidationError

from app.agent.contract import (
    AnswerQuestionInput,
    RetrievalMode,
)
from app.agent.planning.ai.gemini import (
    GeminiQuestionPlannerResponseDefect,
    QuestionPlannerResponseInvalidError,
)
from app.agent.planning.audit import (
    PlannerAttemptFailureEvent,
    PlannerDraftReceivedEvent,
    PlannerFinalEvent,
    PlannerOutcomeCode,
    RequestRetryDisposition,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    NoRetrievalPlan,
    QuestionPlan,
    QuestionPlanDraft,
    QuestionPlanner,
    safe_fallback_plan,
)
from app.agent.planning.flow import (
    QuestionPlanningFlow,
)
from app.analysis.ai_provider_errors import AIProviderNetworkError
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_PLANNER_OUTCOME_METRIC = "vector.agent.planner.outcome"


def _input(question: str = "今日のNVIDIAの発表は？") -> AnswerQuestionInput:
    return AnswerQuestionInput(
        question=question,
        as_of=datetime(2026, 6, 29, tzinfo=UTC),
    )


def _plan(
    mode: RetrievalMode,
    *,
    internal_queries: list[str] | None = None,
    external_research_tasks: list[ExternalResearchTask] | None = None,
    reason: str = "test reason",
) -> QuestionPlan:
    if mode == "internal" and internal_queries is None:
        internal_queries = ["internal query"]
    if mode == "external" and external_research_tasks is None:
        external_research_tasks = [_external_task()]
    if mode == "internal_and_external":
        internal_queries = internal_queries or ["internal query"]
        external_research_tasks = external_research_tasks or [_external_task()]
    match mode:
        case "none":
            return NoRetrievalPlan(reason=reason)
        case "internal":
            return InternalRetrievalPlan(
                internal_queries=internal_queries or [],
                reason=reason,
            )
        case "external":
            return ExternalSearchPlan(
                external_research_tasks=external_research_tasks or [],
                reason=reason,
            )
        case "internal_and_external":
            return InternalAndExternalPlan(
                internal_queries=internal_queries or [],
                external_research_tasks=external_research_tasks or [],
                reason=reason,
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


def _validation_error() -> ValidationError:
    try:
        QuestionPlanDraft(retrieval_mode="none", reason="")
    except ValidationError as exc:
        return exc
    raise AssertionError("expected validation error")


class FakePlanner:
    def __init__(self, outcomes: Sequence[QuestionPlanDraft | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.previous_errors: list[str | None] = []

    async def plan(
        self,
        input: AnswerQuestionInput,
        *,
        previous_error: str | None = None,
    ) -> QuestionPlanDraft:
        self.previous_errors.append(previous_error)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakePlannerAuditRecorder:
    def __init__(self) -> None:
        self.attempt_failures: list[PlannerAttemptFailureEvent] = []
        self.draft_events: list[PlannerDraftReceivedEvent] = []
        self.final_events: list[PlannerFinalEvent] = []
        self.events: list[
            PlannerAttemptFailureEvent | PlannerDraftReceivedEvent | PlannerFinalEvent
        ] = []

    async def record_draft_received(
        self,
        event: PlannerDraftReceivedEvent,
    ) -> None:
        self.draft_events.append(event)
        self.events.append(event)

    async def record_attempt_failure(
        self,
        event: PlannerAttemptFailureEvent,
    ) -> None:
        self.attempt_failures.append(event)
        self.events.append(event)

    async def record_final_event(self, event: PlannerFinalEvent) -> None:
        self.final_events.append(event)
        self.events.append(event)


class RaisingPlannerAuditRecorder:
    async def record_draft_received(
        self,
        event: PlannerDraftReceivedEvent,
    ) -> None:
        raise RuntimeError("audit recorder down")

    async def record_attempt_failure(
        self,
        event: PlannerAttemptFailureEvent,
    ) -> None:
        raise RuntimeError("audit recorder down")

    async def record_final_event(self, event: PlannerFinalEvent) -> None:
        raise RuntimeError("audit recorder down")


def _response_invalid() -> QuestionPlannerResponseInvalidError:
    return QuestionPlannerResponseInvalidError(
        GeminiQuestionPlannerResponseDefect.NOT_JSON
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


class TestQuestionPlanningFlow:
    @pytest.mark.asyncio
    async def test_returns_completed_plan_from_draft(self) -> None:
        planner = FakePlanner(
            [
                _draft(
                    "external",
                    external_collection_goals=["  NVIDIA の直近発表を確認する  "],
                )
            ]
        )

        plan = await QuestionPlanningFlow(planner=planner).plan(_input())

        assert plan.external_research_tasks == [
            _external_task("NVIDIA の直近発表を確認する")
        ]
        assert planner.previous_errors == [None]

    @pytest.mark.asyncio
    async def test_retries_once_with_previous_error(self) -> None:
        repaired = _draft(
            "external",
            external_collection_goals=["NVIDIA の発表根拠を確認する"],
        )
        planner = FakePlanner([_response_invalid(), repaired])

        plan = await QuestionPlanningFlow(planner=planner).plan(_input())

        assert plan.retrieval_mode == "external"
        assert planner.previous_errors[0] is None
        assert planner.previous_errors[1]

    @pytest.mark.asyncio
    async def test_falls_back_after_retry_failure(self) -> None:
        planner = FakePlanner([_response_invalid(), _validation_error()])

        plan = await QuestionPlanningFlow(planner=planner).plan(
            _input("保存済みの記事からAI半導体ニュースをまとめて")
        )

        assert plan == safe_fallback_plan(
            fallback_query="保存済みの記事からAI半導体ニュースをまとめて"
        )
        assert planner.previous_errors[0] is None
        assert planner.previous_errors[1]

    @pytest.mark.asyncio
    async def test_provider_error_falls_back_without_retry_and_records(self) -> None:
        planner = FakePlanner([AIProviderNetworkError()])
        recorder = FakePlannerAuditRecorder()

        plan = await QuestionPlanningFlow(
            planner=planner,
            audit_recorder=recorder,
        ).plan(_input("保存済みの記事からAI半導体ニュースをまとめて"))

        assert plan == safe_fallback_plan(
            fallback_query="保存済みの記事からAI半導体ニュースをまとめて"
        )
        assert planner.previous_errors == [None]
        assert len(recorder.attempt_failures) == 1
        failure = recorder.attempt_failures[0]
        assert failure.attempt_number == 1
        assert (
            failure.request_retry_disposition
            is RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST
        )
        assert failure.failure_kind == "attempt_scoped"
        assert recorder.draft_events == []
        assert len(recorder.final_events) == 1
        final = recorder.final_events[0]
        assert final.outcome_code is PlannerOutcomeCode.FALLBACK_USED
        assert final.attempt_count == 1
        assert final.retry_used is False
        assert final.fallback_used is True

    @pytest.mark.asyncio
    async def test_retry_success_records_attempt_failure_and_final_plan(self) -> None:
        repaired = _draft(
            "external",
            external_collection_goals=["NVIDIA の発表根拠を確認する"],
        )
        planner = FakePlanner([_response_invalid(), repaired])
        recorder = FakePlannerAuditRecorder()

        plan = await QuestionPlanningFlow(
            planner=planner,
            audit_recorder=recorder,
        ).plan(_input())

        assert plan.retrieval_mode == "external"
        assert len(recorder.attempt_failures) == 1
        failure = recorder.attempt_failures[0]
        assert failure.attempt_number == 1
        assert (
            failure.request_retry_disposition
            is RequestRetryDisposition.RETRY_IN_REQUEST
        )
        assert len(recorder.final_events) == 1
        final = recorder.final_events[0]
        assert final.outcome_code is PlannerOutcomeCode.PLAN_CREATED
        assert final.attempt_count == 2
        assert final.retry_used is True
        assert final.fallback_used is False
        assert final.retrieval_mode == "external"
        assert final.external_query_count == len(plan.external_research_tasks)
        assert len(recorder.draft_events) == 1
        draft = recorder.draft_events[0]
        assert draft.outcome_code is PlannerOutcomeCode.DRAFT_RECEIVED
        assert draft.attempt_number == 2
        assert draft.draft_internal_query_count == 0
        assert draft.draft_external_query_count == 1

    @pytest.mark.asyncio
    async def test_plan_created_audit_records_raw_draft_and_capped_final_counts(
        self,
    ) -> None:
        planner = FakePlanner(
            [
                _draft(
                    "internal",
                    internal_queries=[
                        "  NVIDIA AI GPU  ",
                        "nvidia ai gpu",
                        "   ",
                        "OpenAI",
                        "Apple",
                    ],
                )
            ]
        )
        recorder = FakePlannerAuditRecorder()

        plan = await QuestionPlanningFlow(
            planner=planner,
            audit_recorder=recorder,
        ).plan(_input())

        assert isinstance(plan, InternalRetrievalPlan)
        assert plan.internal_queries == [
            "NVIDIA AI GPU",
            "OpenAI",
            "Apple",
        ]
        draft = recorder.draft_events[0]
        assert draft.attempt_number == 1
        assert draft.retrieval_mode == "internal"
        assert draft.draft_internal_query_count == 5
        assert draft.draft_external_query_count == 0
        final = recorder.final_events[0]
        assert final.internal_query_count == 3
        assert final.external_query_count == 0
        assert recorder.events == [draft, final]
        dumped = json.dumps(
            [event.model_dump(mode="json") for event in recorder.events],
            ensure_ascii=False,
            default=str,
        )
        for query_text in ("NVIDIA AI GPU", "nvidia ai gpu", "OpenAI", "Apple"):
            assert query_text not in dumped

    @pytest.mark.asyncio
    async def test_fallback_after_retry_failure_records_two_attempts(self) -> None:
        planner = FakePlanner([_response_invalid(), _validation_error()])
        recorder = FakePlannerAuditRecorder()

        plan = await QuestionPlanningFlow(
            planner=planner,
            audit_recorder=recorder,
        ).plan(_input())

        assert plan == safe_fallback_plan(fallback_query=_input().question)
        assert [event.attempt_number for event in recorder.attempt_failures] == [1, 2]
        assert recorder.draft_events == []
        assert len(recorder.final_events) == 1
        final = recorder.final_events[0]
        assert final.outcome_code is PlannerOutcomeCode.FALLBACK_USED
        assert final.attempt_count == 2
        assert final.retry_used is True
        assert final.fallback_used is True

    @pytest.mark.asyncio
    async def test_recorder_errors_do_not_stop_planning(self) -> None:
        repaired = _draft("internal", internal_queries=["NVIDIA AI GPU"])
        planner = FakePlanner([_response_invalid(), repaired])

        plan = await QuestionPlanningFlow(
            planner=planner,
            audit_recorder=RaisingPlannerAuditRecorder(),
        ).plan(_input())

        assert plan.retrieval_mode == "internal"

        fallback_planner = FakePlanner([AIProviderNetworkError()])
        fallback = await QuestionPlanningFlow(
            planner=fallback_planner,
            audit_recorder=RaisingPlannerAuditRecorder(),
        ).plan(_input("保存済み記事で見て"))

        assert fallback == safe_fallback_plan(fallback_query="保存済み記事で見て")

    @pytest.mark.asyncio
    async def test_non_validation_error_propagates_without_outcome_metric(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        planner = FakePlanner([TimeoutError("provider timeout")])

        with pytest.raises(TimeoutError):
            await QuestionPlanningFlow(planner=planner).plan(_input())
        metrics = collected_metrics(capfire)
        assert _metric_attributes(metrics, _PLANNER_OUTCOME_METRIC) == []

    @pytest.mark.asyncio
    async def test_outcome_metric_records_planned_once(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        planner = FakePlanner([_draft("internal", internal_queries=["NVIDIA"])])

        await QuestionPlanningFlow(planner=planner).plan(
            _input("生の質問テキストを混ぜない")
        )

        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "planned") == 1
        assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "fallback") == 0
        attrs = _metric_attributes(metrics, _PLANNER_OUTCOME_METRIC)
        assert attrs == [
            {
                "result": "planned",
                "retry_used": False,
                "planned_retrieval_mode": "internal",
            }
        ]
        dumped = json.dumps(metrics, default=str, ensure_ascii=False)
        assert "生の質問テキストを混ぜない" not in dumped

    @pytest.mark.asyncio
    async def test_outcome_metric_records_retry_success_once(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        planner = FakePlanner(
            [
                _response_invalid(),
                _draft(
                    "external",
                    external_collection_goals=["NVIDIA の外部根拠を確認する"],
                ),
            ]
        )

        await QuestionPlanningFlow(planner=planner).plan(_input())

        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "planned") == 1
        attrs = _metric_attributes(metrics, _PLANNER_OUTCOME_METRIC)
        assert attrs == [
            {
                "result": "planned",
                "retry_used": True,
                "planned_retrieval_mode": "external",
            }
        ]

    @pytest.mark.asyncio
    async def test_outcome_metric_records_fallback_once(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        planner = FakePlanner([AIProviderNetworkError()])

        await QuestionPlanningFlow(planner=planner).plan(_input())

        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _PLANNER_OUTCOME_METRIC, "fallback") == 1
        attrs = _metric_attributes(metrics, _PLANNER_OUTCOME_METRIC)
        assert attrs == [
            {
                "result": "fallback",
                "retry_used": False,
                "planned_retrieval_mode": "internal",
            }
        ]


class TestPlannerExports:
    def test_question_planner_protocol_is_importable(self) -> None:
        assert QuestionPlanner is not None
