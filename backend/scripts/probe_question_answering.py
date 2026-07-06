"""Probe question answering external retrieval with real adapters."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.answering import QuestionPlanRetrievalService
from app.agent.contract import ExternalResearchTask, QuestionPlan
from app.agent.external_search import (
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    ExternalSearchResearchRunner,
    ExternalSearchService,
    ResearchTaskReport,
    TavilySearchProvider,
)
from app.agent.external_search.ai import (
    DeepSeekEvidenceSelector,
    DeepSeekQueryGenerator,
)
from app.agent.internal_retrieval.article_search import InternalArticleSearchHit
from app.config import settings
from app.shared.security.safe_http import make_safe_async_client

DEFAULT_GOAL = "NVIDIA Blackwell AI GPU latest supply and customer demand evidence"
MAX_EXTERNAL_RESEARCH_TASKS = 3
SNIPPET_DISPLAY_MAX_CHARS = 240


class _UnreachableInternalSearch:
    async def search_plan_articles(
        self,
        plan: QuestionPlan,
    ) -> list[InternalArticleSearchHit]:
        raise AssertionError(
            f"internal search must not be called for {plan.retrieval_mode!r} probe"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe QuestionPlanRetrievalService external retrieval with real "
            "DeepSeek and Tavily adapters."
        )
    )
    parser.add_argument(
        "goals",
        nargs="*",
        metavar="goal",
        help=(
            "External research collection_goal. Quote each goal containing spaces. "
            "At most 3 goals are accepted."
        ),
    )
    parser.add_argument(
        "--agents",
        type=int,
        default=1,
        help="Requested external search agent count. Defaults to 1.",
    )
    parser.add_argument(
        "--time-window",
        default=None,
        help="Optional QuestionPlan.target_time_window value.",
    )
    return parser


async def _probe(
    *,
    goals: Sequence[str],
    requested_agent_count: int,
    target_time_window: str | None,
) -> None:
    _require_secret("TAVILY_API_KEY", settings.tavily_api_key.get_secret_value())
    _require_secret("DEEPSEEK_API_KEY", settings.deepseek_api_key.get_secret_value())

    as_of = datetime.now(UTC)
    plan = _build_external_plan(goals, target_time_window=target_time_window)

    async with make_safe_async_client() as client:
        provider = TavilySearchProvider(
            api_key=settings.tavily_api_key,
            client=client,
        )
        runner = ExternalSearchResearchRunner(
            query_generator=DeepSeekQueryGenerator(),
            search_provider=provider,
            evidence_selector=DeepSeekEvidenceSelector(),
        )
        service = QuestionPlanRetrievalService(
            internal_search=_UnreachableInternalSearch(),
            external_search=ExternalSearchService(runner=runner),
            requested_external_agent_count=requested_agent_count,
        )
        outcome = await service.retrieve(plan, as_of=as_of)

    _print_probe_summary(
        as_of=as_of,
        plan=plan,
        requested_agent_count=requested_agent_count,
        outcome=outcome.external_search,
        unmet_requirements=outcome.unmet_requirements,
    )


def _require_secret(name: str, value: str) -> None:
    if not value:
        raise SystemExit(f"{name} is not configured")


def _build_external_plan(
    goals: Sequence[str],
    *,
    target_time_window: str | None,
) -> QuestionPlan:
    cleaned_goals = [goal.strip() for goal in goals if goal.strip()]
    if not cleaned_goals:
        cleaned_goals = [DEFAULT_GOAL]
    if len(cleaned_goals) > MAX_EXTERNAL_RESEARCH_TASKS:
        raise SystemExit(
            f"external research goals must be at most {MAX_EXTERNAL_RESEARCH_TASKS}"
        )

    return QuestionPlan(
        retrieval_mode="external",
        external_research_tasks=[
            ExternalResearchTask(collection_goal=goal) for goal in cleaned_goals
        ],
        target_time_window=target_time_window,
        reason="external retrieval probe",
    )


def _print_probe_summary(
    *,
    as_of: datetime,
    plan: QuestionPlan,
    requested_agent_count: int,
    outcome: ExternalSearchOutcome | None,
    unmet_requirements: Sequence[str],
) -> None:
    print(f"as_of={as_of.isoformat()}")
    print(f"planned_mode={plan.retrieval_mode}")
    print(f"target_time_window={plan.target_time_window or ''}")
    print(f"requested_agent_count={requested_agent_count}")
    print(f"planned_task_count={len(plan.external_research_tasks)}")

    if outcome is None:
        print("external_search_outcome=None")
        print(f"unmet_requirements={list(unmet_requirements)}")
        return

    print(f"effective_agent_count={outcome.effective_agent_count}")
    print(f"hard_agent_limit={outcome.hard_agent_limit}")
    print(f"outcome_task_count={len(outcome.tasks)}")
    print(f"evidence_count={len(outcome.evidence)}")
    print(f"deduplicated_evidence_count={outcome.deduplicated_evidence_count}")
    print(f"unmet_requirements={list(unmet_requirements)}")
    print()
    _print_task_reports(outcome.task_reports)
    print()
    _print_evidence(outcome.evidence)


def _print_task_reports(reports: Sequence[ResearchTaskReport]) -> None:
    print("task_reports:")
    if not reports:
        print("  (none)")
        return

    for report in reports:
        print(
            "  "
            f"[{report.task_index}] status={report.status} "
            f"candidates={report.candidate_count} "
            f"evidence={report.evidence_count} "
            f"provider_failed_queries={report.provider_failed_query_count} "
            f"dropped_selections={report.dropped_selection_count} "
            f"missing={len(report.missing)}"
        )
        print(f"      goal={report.collection_goal}")
        if report.selector_failure_reason:
            print(f"      selector_failure_reason={report.selector_failure_reason}")
        if report.generated_queries:
            print("      generated_queries:")
            for query in report.generated_queries:
                print(f"        - {query}")
        if report.missing:
            print("      missing:")
            for item in report.missing:
                print(f"        - {item}")


def _print_evidence(evidence_items: Sequence[ExternalSearchEvidence]) -> None:
    print("evidence:")
    if not evidence_items:
        print("  (none)")
        return

    for index, evidence in enumerate(evidence_items, start=1):
        print(
            "  "
            f"[{index}] source_ref={evidence.source_ref} "
            f"task_index={evidence.task_index}"
        )
        print(f"      title={evidence.title}")
        print(f"      url={evidence.url}")
        print(f"      source_name={evidence.source_name or ''}")
        print(f"      published_at={_format_datetime(evidence.published_at)}")
        print(f"      claim={evidence.claim}")
        print(f"      why_selected={evidence.why_selected}")
        if evidence.snippet:
            print(f"      snippet={_truncate_for_display(evidence.snippet)}")


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _truncate_for_display(value: str) -> str:
    if len(value) <= SNIPPET_DISPLAY_MAX_CHARS:
        return value
    return f"{value[:SNIPPET_DISPLAY_MAX_CHARS]}..."


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    asyncio.run(
        _probe(
            goals=args.goals,
            requested_agent_count=args.agents,
            target_time_window=args.time_window,
        )
    )


if __name__ == "__main__":
    main()
