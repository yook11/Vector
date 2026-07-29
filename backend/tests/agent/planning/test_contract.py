"""Question Planner の2 plan domain contract。"""

from __future__ import annotations

import importlib
import inspect
from datetime import date, datetime
from typing import Any, get_args, get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.planning.service import QuestionPlanningService
from app.agent.question_context.contract import QuestionContext
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
    AgentRuntimeScopeFactory,
)


def _contracts() -> Any:
    return importlib.import_module("app.agent.planning.contract")


def _required_contract(name: str) -> Any:
    value = getattr(_contracts(), name, None)
    if value is None:
        pytest.fail(f"planning contract must define {name}")
    return value


def _draft(
    *,
    plan_type: str = "search",
    research_tasks: list[Any] | None = None,
    target_time_window: object | None = None,
) -> Any:
    return _required_contract("QuestionPlanDraft")(
        plan_type=plan_type,
        research_tasks=research_tasks or [],
        target_time_window=target_time_window,
    )


def _task_draft(research_goal: str, article_search_queries: list[str]) -> Any:
    return _required_contract("ResearchTaskDraft")(
        research_goal=research_goal,
        article_search_queries=article_search_queries,
    )


def _research_task(research_goal: str, article_search_queries: list[str]) -> Any:
    return _required_contract("ResearchTask")(
        research_goal=research_goal,
        article_search_queries=article_search_queries,
    )


def _time_window(**payload: object) -> Any:
    return _required_contract("TargetTimeWindow").model_validate(payload)


def _render_time_window(target_time_window: object) -> str:
    renderer = _required_contract("render_target_time_window")
    return renderer(target_time_window)


def _first_input_annotation(method: object) -> object | None:
    parameter_names = tuple(inspect.signature(method).parameters)
    return get_type_hints(method).get(parameter_names[1])


def test_contract_exports_only_direct_and_search_plan_vocabulary() -> None:
    contracts = _contracts()

    assert set(get_args(_required_contract("PlanType"))) == {
        "direct_answer",
        "search",
    }
    assert {
        "PlanType",
        "QuestionPlan",
        "QuestionPlanDraft",
        "DirectAnswerPlan",
        "SearchPlan",
        "plan_from_draft",
    } <= set(vars(contracts))
    assert not any(
        hasattr(contracts, legacy_name)
        for legacy_name in (
            "RetrievalMode",
            "RetrievalPlan",
            "NoRetrievalPlan",
            "InternalRetrievalPlan",
            "ExternalSearchPlan",
            "InternalAndExternalPlan",
            "safe_fallback_plan",
        )
    )


def test_question_plan_union_contains_exactly_direct_and_search_variants() -> None:
    assert set(get_args(_required_contract("QuestionPlan"))) == {
        _required_contract("DirectAnswerPlan"),
        _required_contract("SearchPlan"),
    }


def test_draft_has_exact_new_fields_and_forbids_old_fields() -> None:
    draft_type = _required_contract("QuestionPlanDraft")

    assert set(draft_type.model_fields) == {
        "plan_type",
        "research_tasks",
        "target_time_window",
    }
    assert all(
        draft_type.model_fields[field_name].is_required()
        for field_name in (
            "plan_type",
            "research_tasks",
        )
    )
    assert not draft_type.model_fields["target_time_window"].is_required()

    with pytest.raises(ValidationError):
        draft_type.model_validate(
            {
                "plan_type": "search",
                "research_tasks": [
                    {
                        "research_goal": "NVIDIA の根拠を確認する",
                        "article_search_queries": ["NVIDIA AI GPU"],
                    }
                ],
                "reason": "legacy explanation",
            }
        )
    with pytest.raises(ValidationError):
        draft_type.model_validate(
            {
                "plan_type": "search",
                "research_tasks": [
                    {
                        "research_goal": "NVIDIA の根拠を確認する",
                        "article_search_queries": ["NVIDIA AI GPU"],
                    }
                ],
                "retrieval_mode": "internal_and_external",
                "internal_queries": ["legacy"],
                "external_collection_goals": ["legacy"],
                "article_search_queries": ["legacy top level"],
                "research_goals": ["legacy top level"],
            }
        )


@pytest.mark.parametrize(
    "legacy_plan_type",
    ["none", "internal", "external", "internal_and_external"],
)
def test_draft_rejects_each_legacy_plan_type(legacy_plan_type: str) -> None:
    with pytest.raises(ValidationError):
        _required_contract("QuestionPlanDraft").model_validate(
            {
                "plan_type": legacy_plan_type,
                "research_tasks": [
                    {
                        "research_goal": "NVIDIA の根拠を確認する",
                        "article_search_queries": ["NVIDIA AI GPU"],
                    }
                ],
            }
        )


def test_direct_plan_has_no_search_fields_and_rejects_them_as_extra() -> None:
    direct_plan_type = _required_contract("DirectAnswerPlan")

    assert set(direct_plan_type.model_fields) == {"plan_type"}
    assert direct_plan_type().plan_type == "direct_answer"
    with pytest.raises(ValidationError):
        direct_plan_type(research_tasks=[])


def test_search_plan_rejects_research_tasks_outside_closed_bounds() -> None:
    """research_tasksの件数境界(下限1・上限RESEARCH_TASK_LIMIT)を構造的に拒否する。"""
    search_plan_type = _required_contract("SearchPlan")
    task_limit = _required_contract("RESEARCH_TASK_LIMIT")

    with pytest.raises(ValidationError):
        search_plan_type(research_tasks=[])
    with pytest.raises(ValidationError):
        search_plan_type(
            research_tasks=[
                _research_task(f"goal {index}", [f"query {index}"])
                for index in range(task_limit + 1)
            ]
        )


def test_search_plan_rejects_duplicate_research_goals() -> None:
    search_plan_type = _required_contract("SearchPlan")

    with pytest.raises(ValidationError):
        search_plan_type(
            research_tasks=[
                _research_task("NVIDIA の根拠を確認する", ["NVIDIA"]),
                _research_task("NVIDIA の根拠を確認する", ["Blackwell"]),
            ]
        )


def test_search_plan_allows_the_same_query_text_in_different_tasks() -> None:
    """task間のquery重複は禁じない(task内のcasefold一意はResearchTaskが持つ)。"""
    search_plan_type = _required_contract("SearchPlan")

    plan = search_plan_type(
        research_tasks=[
            _research_task("第一の調査目的", ["共有クエリ"]),
            _research_task("第二の調査目的", ["共有クエリ"]),
        ]
    )

    assert [task.article_search_queries for task in plan.research_tasks] == [
        ["共有クエリ"],
        ["共有クエリ"],
    ]


def test_search_plan_rejects_query_total_over_budget_across_tasks() -> None:
    """MAX_ARTICLE_SEARCH_QUERIESはtask個別の上限ではなくrun全体の合計予算。"""
    search_plan_type = _required_contract("SearchPlan")
    query_budget = _required_contract("MAX_ARTICLE_SEARCH_QUERIES")

    accepted = search_plan_type(
        research_tasks=[
            _research_task("第一の調査目的", ["q1", "q2"]),
            _research_task("第二の調査目的", ["q3"]),
        ]
    )
    assert (
        sum(len(task.article_search_queries) for task in accepted.research_tasks)
        == query_budget
    )

    with pytest.raises(ValidationError):
        search_plan_type(
            research_tasks=[
                _research_task("第一の調査目的", ["q1", "q2"]),
                _research_task("第二の調査目的", ["q3", "q4"]),
            ]
        )


def test_search_plan_has_exact_fields_and_forbids_legacy_extra() -> None:
    search_plan_type = _required_contract("SearchPlan")

    assert set(search_plan_type.model_fields) == {
        "plan_type",
        "research_tasks",
        "target_time_window",
    }
    with pytest.raises(ValidationError):
        search_plan_type(
            research_tasks=[
                _research_task("NVIDIA の根拠を確認する", ["NVIDIA"]),
            ],
            reason="legacy explanation",
        )
    with pytest.raises(ValidationError):
        search_plan_type(
            research_tasks=[
                _research_task("NVIDIA の根拠を確認する", ["NVIDIA"]),
            ],
            article_search_queries=["legacy top level"],
        )


def test_plan_from_draft_normalizes_tasks_and_keeps_typed_time_window() -> None:
    """research_goalの重複排除・task内queryのstrip/casefold重複排除・task上限3を検証する。"""
    plan_from_draft = _required_contract("plan_from_draft")
    draft = _draft(
        research_tasks=[
            _task_draft(
                "  NVIDIA の根拠を確認する  ",
                ["  NVIDIA AI GPU  ", "nvidia ai gpu", "   "],
            ),
            _task_draft(
                "NVIDIA の根拠を確認する",  # 1件目と完全一致goal(先勝ちでskip)
                ["must not appear"],
            ),
            _task_draft("供給需要を確認する", ["半導体供給"]),
            _task_draft("投資影響を確認する", ["投資判断"]),
            _task_draft("4task目は上限で切られる", ["切られるquery"]),
        ],
        target_time_window=_time_window(kind="last_n_days", days=7),
    )

    plan = plan_from_draft(draft)

    assert plan.plan_type == "search"
    assert [task.research_goal for task in plan.research_tasks] == [
        "NVIDIA の根拠を確認する",
        "供給需要を確認する",
        "投資影響を確認する",
    ]
    assert [task.article_search_queries for task in plan.research_tasks] == [
        ["NVIDIA AI GPU"],
        ["半導体供給"],
        ["投資判断"],
    ]
    assert "must not appear" not in [
        query for task in plan.research_tasks for query in task.article_search_queries
    ]
    assert plan.target_time_window == _time_window(kind="last_n_days", days=7)
    assert "fallback_query" not in inspect.signature(plan_from_draft).parameters


@pytest.mark.parametrize(
    ("task_queries", "expected_task_queries"),
    [
        pytest.param(
            [["a", "b"], ["c", "d"]],
            [["a", "b"], ["c"]],
            id="two-tasks-first-task-keeps-two",
        ),
        pytest.param(
            [["a", "b", "c"], ["d"]],
            [["a", "b"], ["d"]],
            id="two-tasks-uneven-lengths",
        ),
        pytest.param(
            [["a"], ["b", "c"], ["d", "e"]],
            [["a"], ["b"], ["d"]],
            id="three-tasks-each-keeps-its-first",
        ),
    ],
)
def test_plan_from_draft_trims_query_budget_by_position_major_round_robin(
    task_queries: list[list[str]],
    expected_task_queries: list[list[str]],
) -> None:
    """予算超過時、query位置ごとにtask順で走査し採用3件で打ち切る決定的trimを検証する。"""
    plan_from_draft = _required_contract("plan_from_draft")
    draft = _draft(
        research_tasks=[
            _task_draft(f"goal {index}", queries)
            for index, queries in enumerate(task_queries)
        ],
    )

    plan = plan_from_draft(draft)

    assert [
        task.article_search_queries for task in plan.research_tasks
    ] == expected_task_queries
    assert all(len(task.article_search_queries) >= 1 for task in plan.research_tasks)
    assert sum(
        len(task.article_search_queries) for task in plan.research_tasks
    ) == _required_contract("MAX_ARTICLE_SEARCH_QUERIES")


def test_plan_from_draft_keeps_duplicate_query_text_present_in_multiple_tasks() -> None:
    """task間の重複queryはdefectにならず両taskに残る(run単位一意性からの緩和)。"""
    plan_from_draft = _required_contract("plan_from_draft")
    draft = _draft(
        research_tasks=[
            _task_draft("第一の調査目的", ["共通クエリ"]),
            _task_draft("第二の調査目的", ["共通クエリ"]),
        ],
    )

    plan = plan_from_draft(draft)

    assert [task.article_search_queries for task in plan.research_tasks] == [
        ["共通クエリ"],
        ["共通クエリ"],
    ]


@pytest.mark.parametrize(
    "draft",
    [
        pytest.param(
            lambda: _draft(
                plan_type="direct_answer",
                research_tasks=[
                    _task_draft(
                        "NVIDIA 概要",
                        ["RAW_QUESTION_MUST_NOT_BE_FALLBACK_34a1"],
                    )
                ],
            ),
            id="direct-with-task",
        ),
        pytest.param(
            lambda: _draft(
                plan_type="direct_answer",
                target_time_window=_time_window(kind="today"),
            ),
            id="direct-with-window",
        ),
        pytest.param(
            lambda: _draft(research_tasks=[]),
            id="search-without-any-task",
        ),
        pytest.param(
            lambda: _draft(research_tasks=[_task_draft("根拠を確認する", ["   ", ""])]),
            id="search-with-empty-task-queries",
        ),
    ],
)
def test_plan_from_draft_turns_semantic_inconsistency_into_safe_response_defect(
    draft: Any,
) -> None:
    plan_from_draft = _required_contract("plan_from_draft")

    with pytest.raises(AgentResponseInvalidError) as raised:
        plan_from_draft(draft())

    assert raised.value.defect is AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH
    assert "RAW_QUESTION_MUST_NOT_BE_FALLBACK_34a1" not in str(raised.value)


def test_direct_draft_creates_direct_answer_plan_only() -> None:
    plan = _required_contract("plan_from_draft")(_draft(plan_type="direct_answer"))

    assert plan.plan_type == "direct_answer"
    assert set(type(plan).model_fields) == {"plan_type"}


def test_direct_and_search_plans_are_frozen() -> None:
    direct = _required_contract("DirectAnswerPlan")()
    search = _required_contract("SearchPlan")(
        research_tasks=[_research_task("根拠を確認する", ["NVIDIA"])],
    )

    with pytest.raises(ValidationError):
        direct.plan_type = "search"
    with pytest.raises(ValidationError):
        search.research_tasks = []


def test_search_plan_has_no_flattening_projection_properties() -> None:
    """保証するテスト条件 19。

    段3でResearcherがtask単位で収集するため、旧consumer向けの平坦化射影
    (article_search_queries / external_research_tasks)は消える(seam終了)。
    """
    search_plan_type = _required_contract("SearchPlan")
    plan = search_plan_type(
        research_tasks=[_research_task("根拠を確認する", ["NVIDIA"])],
    )

    assert (
        hasattr(search_plan_type, "article_search_queries"),
        hasattr(search_plan_type, "external_research_tasks"),
        hasattr(plan, "article_search_queries"),
        hasattr(plan, "external_research_tasks"),
    ) == (False, False, False, False)


def test_planning_request_is_a_frozen_context_consumer_wrapper() -> None:
    request_type = _required_contract("PlanningRequest")
    context = QuestionContext(standalone_question="NVIDIA の直近発表は？")
    as_of = datetime(2026, 7, 10)
    request = request_type(context=context, as_of=as_of)

    with pytest.raises(ValidationError):
        request.as_of = datetime(2026, 7, 11)
    with pytest.raises(ValidationError):
        request_type(context=context, as_of=as_of, telemetry=object())

    assert (
        set(request_type.model_fields),
        request_type.model_fields["context"].annotation,
        request_type.model_fields["as_of"].annotation,
        request.context is context,
        request.as_of,
        "as_of" not in QuestionContext.model_fields,
    ) == (
        {"context", "as_of"},
        QuestionContext,
        datetime,
        True,
        as_of,
        True,
    )


def test_planning_boundaries_accept_planning_request() -> None:
    planner_type = _required_contract("QuestionPlanner")
    request_type = _required_contract("PlanningRequest")

    assert (
        tuple(inspect.signature(planner_type.plan).parameters),
        tuple(inspect.signature(QuestionPlanningService.plan).parameters),
        _first_input_annotation(planner_type.plan),
        _first_input_annotation(QuestionPlanningService.plan),
    ) == (
        ("self", "request"),
        ("self", "request"),
        request_type,
        request_type,
    )


def test_planning_service_declares_agent_and_runtime_scope_dependencies() -> None:
    signature = inspect.signature(QuestionPlanningService.__init__)
    hints = get_type_hints(QuestionPlanningService.__init__)

    assert tuple(signature.parameters) == (
        "self",
        "agent",
        "runtime_scope_factory",
    )
    assert (
        hints["agent"]
        == Agent[
            _required_contract("PlanningAttemptInput"),
            _required_contract("QuestionPlanDraft"),
        ]
    )
    assert hints["runtime_scope_factory"] is AgentRuntimeScopeFactory


def test_legacy_planner_draft_boundaries_are_not_exported() -> None:
    contracts = _contracts()
    assert not hasattr(contracts, "QuestionPlanDraftGenerator")
    assert not hasattr(contracts, "QuestionPlannerResponseInvalidError")

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


class TestExternalResearchTask:
    def test_has_research_goal_only(self) -> None:
        assert set(_required_contract("ExternalResearchTask").model_fields) == {
            "research_goal"
        }

    def test_strips_research_goal(self) -> None:
        task = _required_contract("ExternalResearchTask")(
            research_goal="  NVIDIA の外部根拠を集める  "
        )

        assert task.research_goal == "NVIDIA の外部根拠を集める"

    def test_rejects_blank_research_goal(self) -> None:
        with pytest.raises(ValidationError):
            _required_contract("ExternalResearchTask")(research_goal="   ")

    def test_rejects_legacy_collection_goal_as_extra(self) -> None:
        with pytest.raises(ValidationError):
            _required_contract("ExternalResearchTask").model_validate(
                {
                    "research_goal": "NVIDIA の根拠を確認する",
                    "collection_goal": "legacy field must not be accepted",
                }
            )


class TestResearchTask:
    def test_has_research_goal_and_article_search_queries_only(self) -> None:
        assert set(_required_contract("ResearchTask").model_fields) == {
            "research_goal",
            "article_search_queries",
        }

    def test_strips_research_goal(self) -> None:
        task = _research_task("  NVIDIA の内部検索目的  ", ["NVIDIA"])

        assert task.research_goal == "NVIDIA の内部検索目的"

    def test_rejects_blank_research_goal(self) -> None:
        with pytest.raises(ValidationError):
            _research_task("   ", ["NVIDIA"])

    def test_rejects_empty_query_list(self) -> None:
        with pytest.raises(ValidationError):
            _research_task("NVIDIA の内部検索目的", [])

    def test_rejects_more_than_max_article_search_queries(self) -> None:
        query_budget = _required_contract("MAX_ARTICLE_SEARCH_QUERIES")

        with pytest.raises(ValidationError):
            _research_task(
                "NVIDIA の内部検索目的",
                [f"query {index}" for index in range(query_budget + 1)],
            )

    def test_rejects_casefold_duplicate_queries_within_task(self) -> None:
        with pytest.raises(ValidationError):
            _research_task("NVIDIA の内部検索目的", ["NVIDIA", "nvidia"])


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
    target_time_window = _time_window(**payload)

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
            {"kind": "date_range", "start_date": "2026-06-01"},
            id="missing-end-date",
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 0, "month": 1},
            id="year-underflow",
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 10000, "month": 1},
            id="year-overflow",
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 0},
            id="month-underflow",
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 13},
            id="month-overflow",
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
        _time_window(**payload)


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
        _time_window(**payload)


def test_target_time_window_is_frozen() -> None:
    target_time_window = _time_window(kind="today")

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
    assert _render_time_window(_time_window(**payload)) == expected_display
