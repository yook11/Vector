"""Probe question answering search/synthesis and direct answer paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
)
from app.agent.answering.direct_answer.service import DirectAnswerService
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerInput,
)
from app.agent.answering.evidence_answer.service import EvidenceAnswerService
from app.agent.composition import (
    activate_evidence_reviewer_runtime,
    activate_external_search,
    activate_gemini_agent_runtime,
)
from app.agent.contract import (
    AnswerProgressEvent,
    AnswerQuestionResult,
    AnswerSource,
    EvidenceReviewSelectedEvent,
    ExternalSearchHitsFetchedEvent,
    ExternalSearchQueriesGeneratedEvent,
)
from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.internal_search.ai.gemini import (
    GeminiQueryEmbedder,
)
from app.agent.evidence_collection.internal_search.article_repository import (
    PgVectorArticleSearchRepository,
)
from app.agent.evidence_collection.internal_search.service import (
    InternalSearchService,
)
from app.agent.evidence_review import EvidenceReviewer
from app.agent.planning.contract import (
    DirectAnswerPlan,
    PlanningInput,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.research_handoff.agent import RESEARCH_HANDOFF_AGENT
from app.agent.research_handoff.handoff import ResearchHandoff
from app.agent.research_handoff.handoff_input import ResearchHandoffInput
from app.agent.research_handoff.service import ResearchHandoffService
from app.agent.running import AnsweringPhases, AnsweringRunner, RunIdentity, RunInput
from app.config import settings
from app.db import engine

DEFAULT_GOAL = "NVIDIA Blackwell AI GPU latest supply and customer demand evidence"
DEFAULT_QUESTION = "NVIDIA Blackwell の直近の供給と顧客需要は投資判断に重要？"
DEFAULT_DIRECT_QUESTION = "Vector の使い方を短く教えて"
MAX_EXTERNAL_RESEARCH_TASKS = 3


class _UnreachableInternalSearch:
    async def search(self, queries: object) -> list[object]:
        raise AssertionError(f"internal search must not be called: {queries!r}")


class _FixedSearchPlanner:
    def __init__(self, plan: SearchPlan) -> None:
        self._plan = plan

    async def plan(self, input: PlanningInput) -> SearchPlan:  # noqa: ARG002
        return self._plan


class _FixedDirectPlanner:
    def __init__(self, plan: DirectAnswerPlan) -> None:
        self._plan = plan

    async def plan(self, input: PlanningInput) -> DirectAnswerPlan:  # noqa: ARG002
        return self._plan


class _RecordingAnswerEvents:
    def __init__(self) -> None:
        self.events: list[AnswerProgressEvent] = []

    async def event_occurred(self, event: AnswerProgressEvent) -> None:
        self.events.append(event)


class _UnreachableDirectAnswerer:
    async def answer(self, input: DirectAnswerInput) -> DirectAnswerDraft:
        raise AssertionError(
            f"direct answerer must not be called: {input.request.question!r}"
        )


class _UnreachableExternalSearchScope:
    def __call__(self) -> object:
        raise AssertionError("external search scope must not activate")


class _UnreachableEvidenceReviewerScope:
    def __call__(self) -> object:
        raise AssertionError("evidence reviewer runtime must not activate")


class _UnreachableOrganizer:
    async def organize(self, input: ResearchHandoffInput) -> ResearchHandoff:
        raise AssertionError(f"organizer must not be called: {input.question!r}")


class _UnreachableEvidenceAnswerer:
    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerDraft:
        raise AssertionError(
            f"evidence answerer must not be called: {input.request.question!r}, "
            f"{input.evidence!r}"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe AnsweringRunner search or direct answer path."
    )
    parser.add_argument(
        "--mode",
        choices=("direct", "search"),
        default="search",
        help="Probe mode. Defaults to search.",
    )
    parser.add_argument(
        "goals",
        nargs="*",
        metavar="goal",
        help=(
            "External research goal for --mode search. Quote each goal "
            "containing spaces. At most 3 goals are accepted."
        ),
    )
    parser.add_argument(
        "--time-window",
        type=_parse_target_time_window,
        default=None,
        help="Optional external plan target_time_window value.",
    )
    parser.add_argument(
        "--question",
        default=None,
        help="Question passed to AnsweringRunner.",
    )
    return parser


def _parse_target_time_window(value: str) -> TargetTimeWindow:
    try:
        return TargetTimeWindow.model_validate(json.loads(value))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "time window must be a valid TargetTimeWindow JSON object"
        ) from None


async def _probe(
    *,
    mode: str,
    question: str,
    goals: Sequence[str],
    target_time_window: TargetTimeWindow | None,
) -> None:
    if mode == "direct":
        await _probe_direct(question=question)
        return
    await _probe_search(
        question=question,
        goals=goals,
        target_time_window=target_time_window,
    )


async def _probe_search(
    *,
    question: str,
    goals: Sequence[str],
    target_time_window: TargetTimeWindow | None,
) -> None:
    _require_secret("TAVILY_API_KEY", settings.tavily_api_key.get_secret_value())
    _require_secret("DEEPSEEK_API_KEY", settings.deepseek_api_key.get_secret_value())
    _require_secret("GEMINI_API_KEY", settings.gemini_api_key.get_secret_value())

    as_of = datetime.now(UTC)
    plan = _build_search_plan(
        question,
        goals,
        target_time_window=target_time_window,
    )
    events = _RecordingAnswerEvents()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    internal_search = InternalSearchService(
        embedder=GeminiQueryEmbedder(),
        article_search_repository=PgVectorArticleSearchRepository(session_factory),
    )
    runner = AnsweringRunner(
        phases_factory=lambda: AnsweringPhases(
            planner=_FixedSearchPlanner(plan),
            collector=EvidenceCollectionService(
                internal_search=internal_search,
                events=events,
                external_search_scope_factory=activate_external_search,
            ),
            reviewer=EvidenceReviewer(
                runtime_scope_factory=activate_evidence_reviewer_runtime,
            ),
            evidence_answerer=EvidenceAnswerService(
                agent=EVIDENCE_ANSWER_AGENT,
                runtime_scope_factory=activate_gemini_agent_runtime,
            ),
            direct_answerer=_UnreachableDirectAnswerer(),
            organizer=ResearchHandoffService(
                agent=RESEARCH_HANDOFF_AGENT,
                runtime_scope_factory=activate_gemini_agent_runtime,
            ),
        ),
        events=events,
    )
    result = (
        await runner.run(
            RunInput(question=question, history=()),
            identity=_probe_identity(as_of=as_of),
        )
    ).final_output

    _print_plan_summary(
        as_of=as_of,
        plan=plan,
        plan_type=result.plan_summary.plan_type,
        events=events.events,
    )
    print()
    _print_answer_result(result)


async def _probe_direct(*, question: str) -> None:
    _require_secret("GEMINI_API_KEY", settings.gemini_api_key.get_secret_value())

    as_of = datetime.now(UTC)
    runner = AnsweringRunner(
        phases_factory=lambda: AnsweringPhases(
            planner=_FixedDirectPlanner(DirectAnswerPlan()),
            collector=EvidenceCollectionService(
                internal_search=_UnreachableInternalSearch(),
                external_search_scope_factory=_UnreachableExternalSearchScope(),
            ),
            reviewer=EvidenceReviewer(
                runtime_scope_factory=_UnreachableEvidenceReviewerScope(),
            ),
            evidence_answerer=_UnreachableEvidenceAnswerer(),
            direct_answerer=DirectAnswerService(
                agent=DIRECT_ANSWER_AGENT,
                runtime_scope_factory=activate_gemini_agent_runtime,
            ),
            organizer=_UnreachableOrganizer(),
        ),
    )
    result = (
        await runner.run(
            RunInput(question=question, history=()),
            identity=_probe_identity(as_of=as_of),
        )
    ).final_output

    print("direct:")
    print(f"  as_of={as_of.isoformat()}")
    print("  plan_type=direct_answer")
    print()
    _print_answer_result(result)


def _probe_identity(*, as_of: datetime) -> RunIdentity:
    return RunIdentity(
        user_id=uuid4(),
        run_id=uuid4(),
        thread_id=uuid4(),
        as_of=as_of,
    )


def _require_secret(name: str, value: str) -> None:
    if not value:
        raise SystemExit(f"{name} is not configured")


def _build_search_plan(
    question: str,
    goals: Sequence[str],
    *,
    target_time_window: TargetTimeWindow | None,
) -> SearchPlan:
    cleaned_goals = [goal.strip() for goal in goals if goal.strip()]
    if not cleaned_goals:
        cleaned_goals = [DEFAULT_GOAL]
    if len(cleaned_goals) > MAX_EXTERNAL_RESEARCH_TASKS:
        raise SystemExit(
            f"external research goals must be at most {MAX_EXTERNAL_RESEARCH_TASKS}"
        )

    return SearchPlan(
        research_tasks=[
            ResearchTask(research_goal=goal, article_search_queries=[question])
            for goal in cleaned_goals
        ],
        target_time_window=target_time_window,
    )


def _print_plan_summary(
    *,
    as_of: datetime,
    plan: SearchPlan,
    plan_type: str,
    events: Sequence[AnswerProgressEvent],
) -> None:
    print("plan:")
    print(f"  as_of={as_of.isoformat()}")
    print(f"  plan_type={plan_type}")
    print(f"  target_time_window={plan.target_time_window or ''}")
    print(f"  planned_task_count={len(plan.research_tasks)}")
    print()
    _print_collection_progress(events)
    _print_review_progress(events)


def _print_collection_progress(events: Sequence[AnswerProgressEvent]) -> None:
    print("task_progress:")
    collection_events = [
        event
        for event in events
        if isinstance(
            event,
            ExternalSearchQueriesGeneratedEvent | ExternalSearchHitsFetchedEvent,
        )
    ]
    if not collection_events:
        print("  (none)")
        return

    for event in collection_events:
        match event:
            case ExternalSearchQueriesGeneratedEvent():
                print(f"  [{event.task_index}] generated_queries={list(event.queries)}")
            case ExternalSearchHitsFetchedEvent():
                print(f"  [{event.task_index}] hit_count={event.hit_count}")


def _print_review_progress(events: Sequence[AnswerProgressEvent]) -> None:
    """精査はRun単位1回のため、task単位の収集とは別区分で出す。"""
    print("review_progress:")
    selected = next(
        (event for event in events if isinstance(event, EvidenceReviewSelectedEvent)),
        None,
    )
    if selected is None:
        print("  (none)")
        return
    print(f"  evidence_count={selected.evidence_count}")


def _print_answer_result(result: AnswerQuestionResult) -> None:
    print("answer:")
    print(f"  status={result.status}")
    print(f"  plan_type={result.plan_summary.plan_type}")
    print(f"  answer={result.answer}")
    print(f"  missing_aspects={list(result.missing_aspects)}")
    print("  sources:")
    if not result.sources:
        print("    (none)")
        return
    for source in result.sources:
        _print_answer_source(source)


def _print_answer_source(source: AnswerSource) -> None:
    print(f"    [{source.source_ref}] kind={source.kind}")
    print(f"        title={source.title}")
    url = getattr(source, "url", None)
    if url is not None:
        print(f"        url={url}")
    article_id = getattr(source, "article_id", None)
    if article_id is not None:
        print(f"        article_id={article_id}")
    source_name = getattr(source, "source_name", None)
    print(f"        source_name={source_name or ''}")
    print(f"        published_at={_format_datetime(source.published_at)}")
    evidence_claim = getattr(source, "evidence_claim", None)
    if evidence_claim:
        print(f"        evidence_claim={evidence_claim}")


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    question = args.question or (
        DEFAULT_DIRECT_QUESTION if args.mode == "direct" else DEFAULT_QUESTION
    )
    asyncio.run(
        _probe(
            mode=args.mode,
            question=question,
            goals=args.goals,
            target_time_window=args.time_window,
        )
    )


if __name__ == "__main__":
    main()
