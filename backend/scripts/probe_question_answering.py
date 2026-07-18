"""Probe question answering retrieval/synthesis and direct answer paths."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from openai import AsyncOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.answering.audit import (
    AnswerSynthesisAttemptFailureEvent,
    AnswerSynthesisDefectEvent,
    AnswerSynthesisFinalEvent,
    DirectAnswerAttemptFailureEvent,
    DirectAnswerFinalEvent,
)
from app.agent.answering.direct_answer.ai.gemini import GeminiDirectAnswerGenerator
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.direct_answer.flow import DirectAnswerFlow
from app.agent.answering.evidence_answer.ai.gemini import (
    GeminiEvidenceAnswerDraftGenerator,
)
from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow
from app.agent.answering.orchestration import QuestionAnsweringOrchestrator
from app.agent.contract import AnswerQuestionInput, AnswerQuestionResult, AnswerSource
from app.agent.evidence_collection import (
    EvidenceCollectionOutcome,
    EvidenceCollectionService,
)
from app.agent.evidence_collection.external_search import (
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    ExternalSearchResearchRunner,
    ExternalSearchService,
    ResearchTaskReport,
    TavilySearchProvider,
)
from app.agent.evidence_collection.external_search.agent import (
    EXTERNAL_EVIDENCE_SELECTOR_AGENT,
    EXTERNAL_QUERY_AGENT,
)
from app.agent.evidence_collection.external_search.deepseek_binding import (
    EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
    EXTERNAL_QUERY_DEEPSEEK_BINDING,
)
from app.agent.evidence_collection.internal_search import InternalSearchQueries
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleSearchHit,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    NoRetrievalPlan,
    RetrievalPlan,
)
from app.agent.runtime.deepseek import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
    DeepSeekAgentRuntime,
)
from app.config import settings
from app.shared.security.safe_http import make_safe_async_client

DEFAULT_GOAL = "NVIDIA Blackwell AI GPU latest supply and customer demand evidence"
DEFAULT_QUESTION = "NVIDIA Blackwell の直近の供給と顧客需要は投資判断に重要？"
DEFAULT_DIRECT_QUESTION = "Vector の使い方を短く教えて"
MAX_EXTERNAL_RESEARCH_TASKS = 3
SNIPPET_DISPLAY_MAX_CHARS = 240


class _UnreachableInternalSearch:
    async def search_articles(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalArticleSearchHit]:
        raise AssertionError(f"internal search must not be called: {queries!r}")


class _FixedExternalPlanner:
    def __init__(self, plan: ExternalSearchPlan) -> None:
        self._plan = plan

    async def plan(self, input: AnswerQuestionInput) -> ExternalSearchPlan:  # noqa: ARG002
        return self._plan


class _FixedDirectPlanner:
    def __init__(self, plan: NoRetrievalPlan) -> None:
        self._plan = plan

    async def plan(self, input: AnswerQuestionInput) -> NoRetrievalPlan:  # noqa: ARG002
        return self._plan


class _RecordingEvidenceCollector:
    def __init__(self, evidence_collector: EvidenceCollectionService) -> None:
        self._evidence_collector = evidence_collector
        self.last_outcome: EvidenceCollectionOutcome | None = None

    async def collect(
        self,
        plan: ExternalSearchPlan,
        *,
        as_of: datetime,
    ) -> EvidenceCollectionOutcome:
        outcome = await self._evidence_collector.collect(plan, as_of=as_of)
        self.last_outcome = outcome
        return outcome


class _UnreachableDirectAnswerer:
    async def answer(
        self,
        *,
        question: str,
        as_of: datetime,  # noqa: ARG002
    ) -> DirectAnswerDraft:
        raise AssertionError(f"direct answerer must not be called: {question!r}")


class _UnreachableEvidenceCollector:
    async def collect(
        self,
        plan: RetrievalPlan,
        *,
        as_of: datetime,  # noqa: ARG002
    ) -> EvidenceCollectionOutcome:
        raise AssertionError(f"evidence_collector must not be called: {plan!r}")


class _UnreachableEvidenceAnswerer:
    async def answer(
        self,
        *,
        question: str,
        evidence: list[object],
        as_of: datetime,  # noqa: ARG002
        target_time_window: str | None,  # noqa: ARG002
    ) -> object:
        raise AssertionError(f"evidence answerer must not be called: {question!r}")


class _ProbeSynthesisAuditRecorder:
    def __init__(self) -> None:
        self.attempt_failures: list[AnswerSynthesisAttemptFailureEvent] = []
        self.defects: list[AnswerSynthesisDefectEvent] = []
        self.final_events: list[AnswerSynthesisFinalEvent] = []

    async def record_attempt_failure(
        self,
        event: AnswerSynthesisAttemptFailureEvent,
    ) -> None:
        self.attempt_failures.append(event)

    async def record_defect(self, event: AnswerSynthesisDefectEvent) -> None:
        self.defects.append(event)

    async def record_final_event(self, event: AnswerSynthesisFinalEvent) -> None:
        self.final_events.append(event)


class _ProbeDirectAnswerAuditRecorder:
    def __init__(self) -> None:
        self.attempt_failures: list[DirectAnswerAttemptFailureEvent] = []
        self.final_events: list[DirectAnswerFinalEvent] = []

    async def record_attempt_failure(
        self,
        event: DirectAnswerAttemptFailureEvent,
    ) -> None:
        self.attempt_failures.append(event)

    async def record_final_event(self, event: DirectAnswerFinalEvent) -> None:
        self.final_events.append(event)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe QuestionAnsweringOrchestrator external retrieval/evidence answer "
            "or direct answer path."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("external", "direct"),
        default="external",
        help="Probe mode. Defaults to external.",
    )
    parser.add_argument(
        "goals",
        nargs="*",
        metavar="goal",
        help=(
            "External research collection_goal for --mode external. Quote each goal "
            "containing spaces. At most 3 goals are accepted."
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
        help="Optional external plan target_time_window value.",
    )
    parser.add_argument(
        "--question",
        default=None,
        help="Question passed to QuestionAnsweringOrchestrator.",
    )
    return parser


async def _probe(
    *,
    mode: str,
    question: str,
    goals: Sequence[str],
    requested_agent_count: int,
    target_time_window: str | None,
) -> None:
    if mode == "direct":
        await _probe_direct(question=question)
        return
    await _probe_external(
        question=question,
        goals=goals,
        requested_agent_count=requested_agent_count,
        target_time_window=target_time_window,
    )


async def _probe_external(
    *,
    question: str,
    goals: Sequence[str],
    requested_agent_count: int,
    target_time_window: str | None,
) -> None:
    _require_secret("TAVILY_API_KEY", settings.tavily_api_key.get_secret_value())
    _require_secret("DEEPSEEK_API_KEY", settings.deepseek_api_key.get_secret_value())
    _require_secret("GEMINI_API_KEY", settings.gemini_api_key.get_secret_value())

    as_of = datetime.now(UTC)
    plan = _build_external_plan(goals, target_time_window=target_time_window)
    synthesis_audit = _ProbeSynthesisAuditRecorder()
    deepseek_api_key = settings.deepseek_api_key.get_secret_value()

    async with make_safe_async_client() as client:
        provider = TavilySearchProvider(
            api_key=settings.tavily_api_key,
            client=client,
        )
        runner = ExternalSearchResearchRunner(
            query_agent=EXTERNAL_QUERY_AGENT,
            query_runtime=DeepSeekAgentRuntime(
                client=AsyncOpenAI(
                    api_key=deepseek_api_key,
                    base_url=DEEPSEEK_BASE_URL,
                    timeout=DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
                ),
                binding=EXTERNAL_QUERY_DEEPSEEK_BINDING,
            ),
            search_provider=provider,
            selector_agent=EXTERNAL_EVIDENCE_SELECTOR_AGENT,
            selector_runtime=DeepSeekAgentRuntime(
                client=AsyncOpenAI(
                    api_key=deepseek_api_key,
                    base_url=DEEPSEEK_BASE_URL,
                    timeout=DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
                ),
                binding=EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
            ),
        )
        evidence_collector = _RecordingEvidenceCollector(
            EvidenceCollectionService(
                internal_search=_UnreachableInternalSearch(),
                external_search=ExternalSearchService(runner=runner),
                requested_external_agent_count=requested_agent_count,
            )
        )
        orchestrator = QuestionAnsweringOrchestrator(
            planner=_FixedExternalPlanner(plan),
            evidence_collector=evidence_collector,
            evidence_answerer=EvidenceAnswerFlow(
                generator=GeminiEvidenceAnswerDraftGenerator(),
                audit_recorder=synthesis_audit,
            ),
            direct_answerer=_UnreachableDirectAnswerer(),
        )
        result = await orchestrator.answer(
            AnswerQuestionInput(question=question, as_of=as_of)
        )

    outcome = evidence_collector.last_outcome
    if outcome is None:
        raise SystemExit("retrieval did not run")

    _print_retrieval_summary(
        as_of=as_of,
        plan=plan,
        requested_agent_count=requested_agent_count,
        outcome=outcome.external_search,
        unmet_requirements=outcome.unmet_requirements,
    )
    print()
    _print_answer_result(result)
    print()
    _print_synthesis_audit(synthesis_audit)


async def _probe_direct(*, question: str) -> None:
    _require_secret("GEMINI_API_KEY", settings.gemini_api_key.get_secret_value())

    as_of = datetime.now(UTC)
    direct_audit = _ProbeDirectAnswerAuditRecorder()
    orchestrator = QuestionAnsweringOrchestrator(
        planner=_FixedDirectPlanner(NoRetrievalPlan(reason="direct answer probe")),
        evidence_collector=_UnreachableEvidenceCollector(),
        evidence_answerer=_UnreachableEvidenceAnswerer(),
        direct_answerer=DirectAnswerFlow(
            generator=GeminiDirectAnswerGenerator(),
            audit_recorder=direct_audit,
        ),
    )
    result = await orchestrator.answer(
        AnswerQuestionInput(question=question, as_of=as_of)
    )

    print("direct:")
    print(f"  as_of={as_of.isoformat()}")
    print("  planned_mode=none")
    print()
    _print_answer_result(result)
    print()
    _print_direct_audit(direct_audit)


def _require_secret(name: str, value: str) -> None:
    if not value:
        raise SystemExit(f"{name} is not configured")


def _build_external_plan(
    goals: Sequence[str],
    *,
    target_time_window: str | None,
) -> ExternalSearchPlan:
    cleaned_goals = [goal.strip() for goal in goals if goal.strip()]
    if not cleaned_goals:
        cleaned_goals = [DEFAULT_GOAL]
    if len(cleaned_goals) > MAX_EXTERNAL_RESEARCH_TASKS:
        raise SystemExit(
            f"external research goals must be at most {MAX_EXTERNAL_RESEARCH_TASKS}"
        )

    return ExternalSearchPlan(
        external_research_tasks=[
            ExternalResearchTask(collection_goal=goal) for goal in cleaned_goals
        ],
        target_time_window=target_time_window,
        reason="external retrieval probe",
    )


def _print_retrieval_summary(
    *,
    as_of: datetime,
    plan: ExternalSearchPlan,
    requested_agent_count: int,
    outcome: ExternalSearchOutcome | None,
    unmet_requirements: Sequence[str],
) -> None:
    print("retrieval:")
    print(f"  as_of={as_of.isoformat()}")
    print(f"  planned_mode={plan.retrieval_mode}")
    print(f"  target_time_window={plan.target_time_window or ''}")
    print(f"  requested_agent_count={requested_agent_count}")
    print(f"  planned_task_count={len(plan.external_research_tasks)}")

    if outcome is None:
        print("  external_search_outcome=None")
        print(f"  unmet_requirements={list(unmet_requirements)}")
        return

    print(f"  effective_agent_count={outcome.effective_agent_count}")
    print(f"  hard_agent_limit={outcome.hard_agent_limit}")
    print(f"  outcome_task_count={len(outcome.tasks)}")
    print(f"  evidence_count={len(outcome.evidence)}")
    print(f"  deduplicated_evidence_count={outcome.deduplicated_evidence_count}")
    print(f"  unmet_requirements={list(unmet_requirements)}")
    print()
    _print_task_reports(outcome.task_reports)
    print()
    _print_evidence(outcome.evidence)


def _print_answer_result(result: AnswerQuestionResult) -> None:
    print("answer:")
    print(f"  status={result.status}")
    print(f"  answer={result.answer}")
    print(f"  missing_aspects={list(result.missing_aspects)}")
    print(f"  retrieval_unmet={list(result.retrieval.unmet_requirements)}")
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
    print(f"        source_name={source.source_name or ''}")
    print(f"        published_at={_format_datetime(source.published_at)}")
    if source.snippet:
        print(f"        snippet={_truncate_for_display(source.snippet)}")


def _print_synthesis_audit(recorder: _ProbeSynthesisAuditRecorder) -> None:
    final = recorder.final_events[-1] if recorder.final_events else None
    print("synthesis:")
    print(f"  attempt_failures={len(recorder.attempt_failures)}")
    print(f"  defects={len(recorder.defects)}")
    if final is None:
        print("  final_event=None")
        return
    print(f"  outcome_code={final.outcome_code.value}")
    print(f"  retry_used={final.retry_used}")
    print(f"  fallback_used={final.fallback_used}")
    print(f"  status={final.status}")
    print(f"  defect_count={final.defect_count}")


def _print_direct_audit(recorder: _ProbeDirectAnswerAuditRecorder) -> None:
    final = recorder.final_events[-1] if recorder.final_events else None
    print("direct_answer:")
    print(f"  attempt_failures={len(recorder.attempt_failures)}")
    if final is None:
        print("  final_event=None")
        return
    print(f"  outcome_code={final.outcome_code.value}")
    print(f"  retry_used={final.retry_used}")


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
    question = args.question or (
        DEFAULT_DIRECT_QUESTION if args.mode == "direct" else DEFAULT_QUESTION
    )
    asyncio.run(
        _probe(
            mode=args.mode,
            question=question,
            goals=args.goals,
            requested_agent_count=args.agents,
            target_time_window=args.time_window,
        )
    )


if __name__ == "__main__":
    main()
