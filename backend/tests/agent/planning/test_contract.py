"""Question planning contract tests."""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, date, datetime
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.planning import contract as planning_contract
from app.agent.planning.contract import (
    EXTERNAL_RESEARCH_TASK_LIMIT,
    MAX_INTERNAL_QUERIES,
    ExternalResearchTask,
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    NoRetrievalPlan,
    PlanningAttemptInput,
    QuestionPlanDraft,
    QuestionPlanner,
    plan_from_draft,
    safe_fallback_plan,
)
from app.agent.planning.service import QuestionPlanningService
from app.agent.question_context.contract import QuestionContext
from app.agent.runtime.contract import AgentRuntimeScopeFactory


def _external_task(
    collection_goal: str = "NVIDIA の最新発表を確認する",
) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=collection_goal)


def _request_type(module_name: str, type_name: str) -> type[object]:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"{module_name} must define {type_name}: {exc}")
    request_type = getattr(module, type_name, None)
    if request_type is None:
        pytest.fail(f"{module_name} must define {type_name}")
    return request_type


def _target_time_window_type() -> type[object]:
    return _request_type("app.agent.planning.contract", "TargetTimeWindow")


def _target_time_window(**payload: object) -> object:
    return _target_time_window_type().model_validate(payload)


def _render_target_time_window(target_time_window: object) -> str:
    renderer = getattr(planning_contract, "render_target_time_window", None)
    if renderer is None:
        pytest.fail("planning contract must define render_target_time_window")
    return renderer(target_time_window)


def _first_input_annotation(method: object) -> object | None:
    parameter_names = tuple(inspect.signature(method).parameters)
    return get_type_hints(method).get(parameter_names[1])


def test_planning_request_is_a_frozen_context_consumer_wrapper() -> None:
    request_type = _request_type("app.agent.planning.contract", "PlanningRequest")
    context = QuestionContext(standalone_question="NVIDIA の直近発表は？")
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    request = request_type(context=context, as_of=as_of)

    with pytest.raises(ValidationError):
        request.as_of = datetime(2026, 7, 11, tzinfo=UTC)
    with pytest.raises(ValidationError):
        request_type(context=context, as_of=as_of, telemetry=object())

    assert (
        set(request_type.model_fields),
        request_type.model_fields["context"].annotation,
        request_type.model_fields["as_of"].annotation,
        request.context is context,
        request.context,
        request.as_of,
        "as_of" not in QuestionContext.model_fields,
    ) == (
        {"context", "as_of"},
        QuestionContext,
        datetime,
        True,
        context,
        as_of,
        True,
    )


def test_planning_boundaries_accept_planning_request() -> None:
    assert (
        tuple(inspect.signature(QuestionPlanner.plan).parameters),
        tuple(inspect.signature(QuestionPlanningService.plan).parameters),
        _first_input_annotation(QuestionPlanner.plan),
        _first_input_annotation(QuestionPlanningService.plan),
    ) == (
        ("self", "request"),
        ("self", "request"),
        _request_type("app.agent.planning.contract", "PlanningRequest"),
        _request_type("app.agent.planning.contract", "PlanningRequest"),
    )


def test_planning_service_declares_agent_and_runtime_scope_dependencies() -> None:
    signature = inspect.signature(QuestionPlanningService.__init__)
    hints = get_type_hints(QuestionPlanningService.__init__)

    assert tuple(signature.parameters) == (
        "self",
        "agent",
        "runtime_scope_factory",
    )
    assert hints["agent"] == Agent[PlanningAttemptInput, QuestionPlanDraft]
    assert hints["runtime_scope_factory"] is AgentRuntimeScopeFactory


def test_legacy_planner_draft_boundary_and_error_are_not_exported() -> None:
    assert not hasattr(planning_contract, "QuestionPlanDraftGenerator")
    assert not hasattr(planning_contract, "QuestionPlannerResponseInvalidError")

    legacy_names = {
        "GeminiQuestionPlanner",
        "GeminiQuestionPlannerResponseDefect",
        "GeminiQuestionPlannerSpec",
        "GeminiQuestionPlannerPrompt",
        "QuestionPlanDraftGenerator",
        "QuestionPlannerResponseInvalidError",
    }
    for package_name in (
        "app.agent",
        "app.agent.planning",
        "app.agent.planning.ai",
    ):
        package = importlib.import_module(package_name)
        assert all(not hasattr(package, name) for name in legacy_names)

    for module_name, class_name in (
        ("app.agent.planning.ai.gemini", "GeminiQuestionPlanner"),
        ("app.agent.planning.ai.gemini_spec", "GeminiQuestionPlannerSpec"),
        ("app.agent.planning.ai.gemini_prompt", "GeminiQuestionPlannerPrompt"),
    ):
        try:
            legacy_module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
        else:
            assert not hasattr(legacy_module, class_name)


class TestExternalResearchTask:
    def test_has_collection_goal_only(self) -> None:
        assert set(ExternalResearchTask.model_fields) == {"collection_goal"}

    def test_strips_collection_goal(self) -> None:
        task = ExternalResearchTask(
            collection_goal="  NVIDIA の外部根拠を集める  ",
        )

        assert task.collection_goal == "NVIDIA の外部根拠を集める"

    def test_rejects_blank_collection_goal(self) -> None:
        with pytest.raises(ValidationError):
            ExternalResearchTask(collection_goal="   ")


class TestQuestionPlanVariants:
    def test_no_retrieval_plan_rejects_retrieval_fields(self) -> None:
        with pytest.raises(ValidationError):
            NoRetrievalPlan(
                internal_queries=["ignored"],
                reason="検索不要",
            )

    def test_internal_plan_strips_queries(self) -> None:
        plan = InternalRetrievalPlan(
            internal_queries=["  NVIDIA  "],
            reason="内部記事が必要",
        )

        assert plan.retrieval_mode == "internal"
        assert plan.internal_queries == ["NVIDIA"]

    def test_internal_plan_rejects_empty_or_blank_queries(self) -> None:
        with pytest.raises(ValidationError):
            InternalRetrievalPlan(internal_queries=[], reason="内部記事が必要")
        with pytest.raises(ValidationError):
            InternalRetrievalPlan(internal_queries=["   "], reason="内部記事が必要")

    def test_internal_plan_rejects_queries_over_limit(self) -> None:
        InternalRetrievalPlan(
            internal_queries=[
                f"内部検索 {index}" for index in range(MAX_INTERNAL_QUERIES)
            ],
            reason="内部記事が必要",
        )

        with pytest.raises(ValidationError):
            InternalRetrievalPlan(
                internal_queries=[
                    f"内部検索 {index}" for index in range(MAX_INTERNAL_QUERIES + 1)
                ],
                reason="内部記事が必要",
            )

    def test_internal_plan_rejects_external_fields(self) -> None:
        with pytest.raises(ValidationError):
            InternalRetrievalPlan(
                internal_queries=["NVIDIA"],
                external_research_tasks=[_external_task()],
                reason="内部記事が必要",
            )

    def test_external_plan_rejects_empty_tasks(self) -> None:
        with pytest.raises(ValidationError):
            ExternalSearchPlan(
                external_research_tasks=[],
                reason="外部ニュースが必要",
            )

    def test_external_plan_rejects_duplicate_task_goals(self) -> None:
        with pytest.raises(ValidationError):
            ExternalSearchPlan(
                external_research_tasks=[
                    _external_task("NVIDIA の発表を確認する"),
                    _external_task("NVIDIA の発表を確認する"),
                ],
                reason="外部ニュースが必要",
            )

    def test_external_plan_rejects_tasks_over_limit(self) -> None:
        with pytest.raises(ValidationError):
            ExternalSearchPlan(
                external_research_tasks=[
                    _external_task(f"外部根拠を確認する {index}")
                    for index in range(EXTERNAL_RESEARCH_TASK_LIMIT + 1)
                ],
                reason="外部ニュースが必要",
            )

    def test_internal_and_external_plan_requires_both_inputs(self) -> None:
        with pytest.raises(ValidationError):
            InternalAndExternalPlan(
                internal_queries=[],
                external_research_tasks=[_external_task()],
                reason="両方必要",
            )
        with pytest.raises(ValidationError):
            InternalAndExternalPlan(
                internal_queries=["NVIDIA"],
                external_research_tasks=[],
                reason="両方必要",
            )

    def test_variants_are_frozen(self) -> None:
        plan = NoRetrievalPlan(reason="検索不要")

        with pytest.raises(ValidationError):
            plan.reason = "変更不可"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"kind": "today"}, id="today"),
        pytest.param({"kind": "yesterday"}, id="yesterday"),
        pytest.param({"kind": "last_n_days", "days": 1}, id="last-n-days"),
        pytest.param({"kind": "this_week"}, id="this-week"),
        pytest.param({"kind": "last_week"}, id="last-week"),
        pytest.param({"kind": "this_month"}, id="this-month"),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 6},
            id="calendar-month",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-06-01",
                "end_date_inclusive": "2026-06-15",
            },
            id="date-range",
        ),
        pytest.param(
            {"kind": "unsupported_explicit_window"},
            id="unsupported-explicit-window",
        ),
    ],
)
def test_target_time_window_accepts_each_closed_kind(
    payload: dict[str, object],
) -> None:
    target_time_window = _target_time_window(**payload)

    assert target_time_window.model_dump() == {
        "kind": payload["kind"],
        "year": payload.get("year"),
        "month": payload.get("month"),
        "days": payload.get("days"),
        "start_date": (
            date.fromisoformat(payload["start_date"])
            if "start_date" in payload
            else None
        ),
        "end_date_inclusive": (
            date.fromisoformat(payload["end_date_inclusive"])
            if "end_date_inclusive" in payload
            else None
        ),
    }


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"kind": "unknown"}, id="unknown-kind"),
        pytest.param({"kind": "today", "extra": "forbidden"}, id="extra-field"),
        pytest.param({"kind": "calendar_month", "month": 6}, id="missing-year"),
        pytest.param({"kind": "calendar_month", "year": 2026}, id="missing-month"),
        pytest.param({"kind": "last_n_days"}, id="missing-days"),
        pytest.param(
            {"kind": "date_range", "end_date_inclusive": "2026-06-01"},
            id="missing-start-date",
        ),
        pytest.param(
            {"kind": "date_range", "start_date": "2026-06-01"}, id="missing-end-date"
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 0, "month": 1}, id="year-underflow"
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 10000, "month": 1}, id="year-overflow"
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 0}, id="month-underflow"
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 13}, id="month-overflow"
        ),
        pytest.param({"kind": "last_n_days", "days": 0}, id="days-underflow"),
        pytest.param({"kind": "last_n_days", "days": 61}, id="days-overflow"),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "invalid-date",
                "end_date_inclusive": "2026-06-01",
            },
            id="invalid-iso-date",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026/06/01",
                "end_date_inclusive": "2026-06-01",
            },
            id="non-iso-date",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-06-16",
                "end_date_inclusive": "2026-06-15",
            },
            id="reversed-date-range",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "9999-12-30",
                "end_date_inclusive": "9999-12-31",
            },
            id="date-max-cannot-form-half-open-range",
        ),
    ],
)
def test_target_time_window_rejects_values_outside_its_closed_contract(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _target_time_window(**payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"kind": "today", "year": 2026}, id="year-outside-calendar-month"),
        pytest.param({"kind": "today", "month": 6}, id="month-outside-calendar-month"),
        pytest.param({"kind": "today", "days": 7}, id="days-outside-last-n-days"),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 6, "days": 7},
            id="days-outside-calendar-month",
        ),
        pytest.param(
            {"kind": "last_n_days", "days": 7, "year": 2026},
            id="year-outside-last-n-days",
        ),
        pytest.param(
            {"kind": "today", "start_date": "2026-06-01"},
            id="start-date-outside-date-range",
        ),
        pytest.param(
            {"kind": "today", "end_date_inclusive": "2026-06-01"},
            id="end-date-outside-date-range",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-06-01",
                "end_date_inclusive": "2026-06-15",
                "days": 7,
            },
            id="days-outside-date-range",
        ),
        pytest.param(
            {"kind": "unsupported_explicit_window", "days": 7},
            id="sentinel-cannot-carry-parameters",
        ),
    ],
)
def test_target_time_window_rejects_kind_dependent_fields_on_other_kinds(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _target_time_window(**payload)


def test_target_time_window_is_frozen() -> None:
    target_time_window = _target_time_window(kind="today")

    with pytest.raises(ValidationError):
        target_time_window.kind = "yesterday"


@pytest.mark.parametrize(
    ("payload", "expected_display"),
    [
        pytest.param({"kind": "today"}, "今日", id="today"),
        pytest.param({"kind": "yesterday"}, "昨日", id="yesterday"),
        pytest.param({"kind": "last_n_days", "days": 1}, "直近24時間", id="one-day"),
        pytest.param({"kind": "last_n_days", "days": 3}, "直近3日", id="three-days"),
        pytest.param({"kind": "last_n_days", "days": 7}, "直近7日", id="seven-days"),
        pytest.param({"kind": "last_n_days", "days": 30}, "直近30日", id="thirty-days"),
        pytest.param({"kind": "last_n_days", "days": 60}, "直近60日", id="sixty-days"),
        pytest.param({"kind": "this_week"}, "今週", id="this-week"),
        pytest.param({"kind": "last_week"}, "先週", id="last-week"),
        pytest.param({"kind": "this_month"}, "今月", id="this-month"),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 6},
            "2026年6月",
            id="calendar-month",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-06-01",
                "end_date_inclusive": "2026-06-15",
            },
            "2026年6月1日から2026年6月15日まで",
            id="date-range",
        ),
        pytest.param(
            {"kind": "unsupported_explicit_window"},
            "対応外の明示期間",
            id="unsupported-explicit-window",
        ),
    ],
)
def test_target_time_window_display_is_deterministic_for_each_kind(
    payload: dict[str, object],
    expected_display: str,
) -> None:
    assert (
        _render_target_time_window(_target_time_window(**payload)) == expected_display
    )


class TestPlanFromDraft:
    def test_none_ignores_queries_and_time_window(self) -> None:
        plan = plan_from_draft(
            QuestionPlanDraft(
                retrieval_mode="none",
                internal_queries=["ignored"],
                external_collection_goals=["ignored"],
                target_time_window=_target_time_window(kind="last_n_days", days=1),
                reason="検索不要",
            ),
            fallback_query="fallback",
        )

        assert isinstance(plan, NoRetrievalPlan)
        assert "internal_queries" not in type(plan).model_fields
        assert "external_research_tasks" not in type(plan).model_fields
        assert "target_time_window" not in type(plan).model_fields

    def test_internal_uses_fallback_when_query_missing(self) -> None:
        plan = plan_from_draft(
            QuestionPlanDraft(
                retrieval_mode="internal",
                internal_queries=["  "],
                external_collection_goals=["ignored"],
                reason="内部記事が必要",
            ),
            fallback_query="保存済みの記事からAI半導体ニュースをまとめて",
        )

        assert isinstance(plan, InternalRetrievalPlan)
        assert plan.internal_queries == ["保存済みの記事からAI半導体ニュースをまとめて"]
        assert "external_research_tasks" not in type(plan).model_fields

    def test_internal_queries_drop_blanks_deduplicate_and_clamp(
        self,
    ) -> None:
        plan = plan_from_draft(
            QuestionPlanDraft(
                retrieval_mode="internal",
                internal_queries=[
                    "  NVIDIA AI GPU  ",
                    "  ",
                    "nvidia ai gpu",
                    "Blackwell supply chain",
                    "OpenAI",
                    "Apple",
                ],
                reason="内部記事が必要",
            ),
            fallback_query="fallback",
        )

        assert isinstance(plan, InternalRetrievalPlan)
        assert plan.internal_queries == [
            "NVIDIA AI GPU",
            "Blackwell supply chain",
            "OpenAI",
        ]

    def test_external_uses_fallback_when_goal_missing(self) -> None:
        plan = plan_from_draft(
            QuestionPlanDraft(
                retrieval_mode="external",
                internal_queries=["ignored"],
                external_collection_goals=["  "],
                reason="外部ニュースが必要",
            ),
            fallback_query="今日のNVIDIAの発表は？",
        )

        assert isinstance(plan, ExternalSearchPlan)
        assert plan.external_research_tasks == [
            ExternalResearchTask(collection_goal="今日のNVIDIAの発表は？")
        ]
        assert "internal_queries" not in type(plan).model_fields

    def test_external_goals_drop_blanks_deduplicate_and_clamp(self) -> None:
        plan = plan_from_draft(
            QuestionPlanDraft(
                retrieval_mode="external",
                external_collection_goals=[
                    "  ",
                    "  NVIDIA の直近発表を確認する  ",
                    "NVIDIA の直近発表を確認する",
                    "NVIDIA の供給需要を確認する",
                    "NVIDIA の投資影響を確認する",
                    "NVIDIA の規制影響を確認する",
                ],
                reason="外部ニュースが必要",
            ),
            fallback_query="fallback",
        )

        assert isinstance(plan, ExternalSearchPlan)
        assert plan.external_research_tasks == [
            ExternalResearchTask(collection_goal="NVIDIA の直近発表を確認する"),
            ExternalResearchTask(collection_goal="NVIDIA の供給需要を確認する"),
            ExternalResearchTask(collection_goal="NVIDIA の投資影響を確認する"),
        ]

    def test_internal_and_external_fills_both_queries(self) -> None:
        plan = plan_from_draft(
            QuestionPlanDraft(
                retrieval_mode="internal_and_external",
                reason="両方必要",
            ),
            fallback_query="内部記事と最新ニュースを合わせて整理して",
        )

        assert isinstance(plan, InternalAndExternalPlan)
        assert plan.internal_queries == ["内部記事と最新ニュースを合わせて整理して"]
        assert plan.external_research_tasks == [
            ExternalResearchTask(
                collection_goal="内部記事と最新ニュースを合わせて整理して",
            )
        ]

    def test_internal_and_external_clamps_internal_and_external_inputs(self) -> None:
        plan = plan_from_draft(
            QuestionPlanDraft(
                retrieval_mode="internal_and_external",
                internal_queries=[
                    "  NVIDIA  ",
                    "nvidia",
                    "OpenAI",
                    "Apple",
                    "Google",
                ],
                external_collection_goals=[
                    "  NVIDIA の直近発表を確認する  ",
                    "NVIDIA の直近発表を確認する",
                    "NVIDIA の供給需要を確認する",
                    "NVIDIA の投資影響を確認する",
                    "NVIDIA の規制影響を確認する",
                ],
                reason="両方必要",
            ),
            fallback_query="fallback",
        )

        assert isinstance(plan, InternalAndExternalPlan)
        assert plan.internal_queries == ["NVIDIA", "OpenAI", "Apple"]
        assert plan.external_research_tasks == [
            ExternalResearchTask(collection_goal="NVIDIA の直近発表を確認する"),
            ExternalResearchTask(collection_goal="NVIDIA の供給需要を確認する"),
            ExternalResearchTask(collection_goal="NVIDIA の投資影響を確認する"),
        ]

    @pytest.mark.parametrize(
        "retrieval_mode",
        ["external", "internal_and_external"],
    )
    def test_external_variants_preserve_the_typed_time_window_from_draft(
        self,
        retrieval_mode: str,
    ) -> None:
        target_time_window = _target_time_window(
            kind="date_range",
            start_date="2026-06-01",
            end_date_inclusive="2026-06-15",
        )
        plan = plan_from_draft(
            QuestionPlanDraft(
                retrieval_mode=retrieval_mode,
                internal_queries=["NVIDIA"],
                external_collection_goals=["NVIDIA の発表を確認する"],
                target_time_window=target_time_window,
                reason="外部根拠が必要",
            ),
            fallback_query="fallback",
        )

        assert plan.target_time_window == target_time_window

    def test_safe_fallback_defaults_to_internal(self) -> None:
        plan = safe_fallback_plan(fallback_query="こんにちは")

        assert isinstance(plan, InternalRetrievalPlan)
        assert plan.retrieval_mode == "internal"
        assert plan.internal_queries == ["こんにちは"]

    def test_from_draft_rejects_unreachable_retrieval_mode(self) -> None:
        draft = QuestionPlanDraft.model_construct(
            retrieval_mode="invalid",
            internal_queries=[],
            external_collection_goals=[],
            reason="invalid",
        )

        with pytest.raises(AssertionError):
            plan_from_draft(draft, fallback_query="fallback")
