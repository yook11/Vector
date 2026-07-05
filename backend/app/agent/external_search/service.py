"""Agent-facing external search orchestration boundary."""

from __future__ import annotations

from datetime import datetime

from app.agent.contract import QuestionPlan
from app.agent.external_search.contract import (
    EXTERNAL_SEARCH_AGENT_HARD_LIMIT,
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    ExternalSearchRequest,
    ExternalSearchRunner,
)

__all__ = [
    "ExternalSearchService",
    "resolve_external_search_agent_count",
]


class ExternalSearchService:
    """Plan の external research tasks を最大 3 並列の runner 実行へ丸める。"""

    def __init__(self, *, runner: ExternalSearchRunner) -> None:
        self._runner = runner

    async def search_plan(
        self,
        plan: QuestionPlan,
        *,
        as_of: datetime,
        requested_agent_count: int | None = None,
    ) -> ExternalSearchOutcome:
        if plan.retrieval_mode not in {"external", "internal_and_external"}:
            return ExternalSearchOutcome()

        tasks = plan.external_research_tasks
        effective_agent_count = resolve_external_search_agent_count(
            task_count=len(tasks),
            requested_agent_count=requested_agent_count,
        )
        if not tasks or effective_agent_count == 0:
            return ExternalSearchOutcome(
                tasks=tasks,
                requested_agent_count=requested_agent_count,
                effective_agent_count=effective_agent_count,
            )

        request = ExternalSearchRequest(
            tasks=tasks,
            requested_agent_count=requested_agent_count,
            effective_agent_count=effective_agent_count,
            as_of=as_of,
            target_time_window=plan.target_time_window,
        )
        run_result = await self._runner.search(request)
        evidence, deduplicated_evidence_count = _deduplicate_evidence_by_url(
            run_result.evidence
        )
        return ExternalSearchOutcome(
            tasks=tasks,
            evidence=evidence,
            task_reports=run_result.task_reports,
            deduplicated_evidence_count=deduplicated_evidence_count,
            requested_agent_count=requested_agent_count,
            effective_agent_count=effective_agent_count,
        )


def resolve_external_search_agent_count(
    *,
    task_count: int,
    requested_agent_count: int | None = None,
) -> int:
    """設定値を hard limit 3 と task 数で丸めた実効 agent 数にする。"""

    if task_count <= 0:
        return 0

    requested = task_count if requested_agent_count is None else requested_agent_count
    safe_requested = max(1, requested)
    return min(task_count, safe_requested, EXTERNAL_SEARCH_AGENT_HARD_LIMIT)


def _deduplicate_evidence_by_url(
    evidence: list[ExternalSearchEvidence],
) -> tuple[list[ExternalSearchEvidence], int]:
    deduplicated: list[ExternalSearchEvidence] = []
    seen_urls: set[str] = set()
    dropped_count = 0
    for item in evidence:
        url = str(item.url)
        if url in seen_urls:
            dropped_count += 1
            continue
        deduplicated.append(item)
        seen_urls.add(url)
    return deduplicated, dropped_count
