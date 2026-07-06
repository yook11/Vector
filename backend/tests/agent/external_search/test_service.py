"""External search service orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

import app.agent.external_search as external_search_module
from app.agent.external_search import (
    ExternalSearchRequest,
    ExternalSearchService,
    resolve_external_search_agent_count,
)
from app.agent.planning.contract import ExternalResearchTask


def _as_of() -> datetime:
    return datetime(2026, 7, 4, 9, 0, tzinfo=UTC)


def _task(
    collection_goal: str = "NVIDIA のAI GPU最新根拠を集める",
) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=collection_goal)


def _model(name: str) -> Any:
    return getattr(external_search_module, name)


def _report(
    *,
    task_index: int,
    collection_goal: str | None = None,
    generated_queries: list[str] | None = None,
    status: str = "succeeded",
    provider_failed_query_count: int = 0,
    candidate_count: int = 0,
    evidence_count: int = 0,
    dropped_selection_count: int = 0,
    missing: list[str] | None = None,
) -> Any:
    return _model("ResearchTaskReport")(
        task_index=task_index,
        collection_goal=collection_goal or f"task {task_index}",
        generated_queries=generated_queries or [],
        status=status,
        provider_failed_query_count=provider_failed_query_count,
        candidate_count=candidate_count,
        evidence_count=evidence_count,
        dropped_selection_count=dropped_selection_count,
        missing=missing or [],
    )


def _evidence(
    *,
    task_index: int = 0,
    source_ref: str = "external-0-0",
    url: str = "https://example.com/news",
    title: str = "NVIDIA news",
    claim: str = "NVIDIA announced a new GPU.",
    why_selected: str = "It is a primary evidence candidate.",
) -> Any:
    return external_search_module.ExternalSearchEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim=claim,
        why_selected=why_selected,
        url=url,
        title=title,
        snippet="NVIDIA announced a new GPU.",
        source_name="Example",
    )


def _unsafe_evidence(
    *,
    task_index: int = 0,
    source_ref: str = "external-0-0",
    url: str = "https://example.com/news",
) -> Any:
    return external_search_module.ExternalSearchEvidence.model_construct(
        source_ref=source_ref,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        url=url,
        title="title",
    )


def _run_result(
    *,
    evidence: list[Any] | None = None,
    task_reports: list[Any] | None = None,
) -> Any:
    return _model("ExternalSearchRunResult")(
        evidence=evidence or [],
        task_reports=task_reports or [],
    )


class FakeExternalSearchRunner:
    def __init__(self, run_result: Any | None = None) -> None:
        self.requests: list[ExternalSearchRequest] = []
        self._run_result = run_result

    async def search(self, request: ExternalSearchRequest) -> Any:
        self.requests.append(request)
        if self._run_result is not None:
            return self._run_result
        return _run_result(
            task_reports=[
                _report(
                    task_index=index,
                    collection_goal=task.collection_goal,
                )
                for index, task in enumerate(request.tasks)
            ],
        )


@pytest.mark.parametrize(
    ("task_count", "requested", "expected"),
    [
        (0, None, 0),
        (1, None, 1),
        (2, None, 2),
        (4, None, 3),
        (4, 4, 3),
        (1, 3, 1),
        (2, 0, 1),
        (2, -1, 1),
    ],
)
def test_resolve_external_search_agent_count_clamps_to_safe_range(
    task_count: int,
    requested: int | None,
    expected: int,
) -> None:
    assert (
        resolve_external_search_agent_count(
            task_count=task_count,
            requested_agent_count=requested,
        )
        == expected
    )


@pytest.mark.asyncio
async def test_search_builds_outcome_from_run_result_and_reports() -> None:
    tasks = [
        _task("AI GPU 最新根拠を集める"),
        _task("Blackwell の最新根拠を集める"),
        _task("データセンターGPU発表の根拠を集める"),
    ]
    reports = [
        _report(
            task_index=index,
            collection_goal=task.collection_goal,
            evidence_count=1 if index == 0 else 0,
        )
        for index, task in enumerate(tasks)
    ]
    evidence = [_evidence(task_index=0, source_ref="external-0-0")]
    runner = FakeExternalSearchRunner(
        _run_result(evidence=evidence, task_reports=reports)
    )
    service = ExternalSearchService(runner=runner)

    outcome = await service.search(
        tasks,
        target_time_window="直近24時間",
        as_of=_as_of(),
        requested_agent_count=4,
    )

    assert len(runner.requests) == 1
    request = runner.requests[0]
    assert (
        request.tasks == tasks
        and request.requested_agent_count == 4
        and request.effective_agent_count == 3
        and request.as_of == _as_of()
        and request.target_time_window == "直近24時間"
        and outcome.requested_agent_count == 4
        and outcome.effective_agent_count == 3
        and outcome.tasks == tasks
        and outcome.evidence == evidence
        and outcome.task_reports == reports
        and outcome.deduplicated_evidence_count == 0
    )


@pytest.mark.asyncio
async def test_search_defaults_count_to_task_count_with_cap() -> None:
    tasks = [
        _task("AI GPU 最新根拠を集める"),
        _task("Blackwell の最新根拠を集める"),
        _task("データセンターGPU発表の根拠を集める"),
    ]
    runner = FakeExternalSearchRunner()
    service = ExternalSearchService(runner=runner)

    await service.search(
        tasks[:2],
        target_time_window=None,
        as_of=_as_of(),
    )
    await service.search(
        tasks,
        target_time_window=None,
        as_of=_as_of(),
    )

    assert [request.effective_agent_count for request in runner.requests] == [2, 3]


@pytest.mark.asyncio
async def test_search_skips_runner_when_tasks_are_empty() -> None:
    runner = FakeExternalSearchRunner()
    service = ExternalSearchService(runner=runner)

    outcome = await service.search(
        [],
        target_time_window=None,
        as_of=_as_of(),
    )

    assert (
        runner.requests == []
        and outcome.evidence == []
        and outcome.task_reports == []
        and outcome.effective_agent_count == 0
    )


@pytest.mark.asyncio
async def test_search_deduplicates_cross_task_urls_without_rewriting_refs() -> (
    None
):
    tasks = [_task("需要面を調べる"), _task("供給面を調べる")]
    reports = [
        _report(
            task_index=0,
            collection_goal=tasks[0].collection_goal,
            evidence_count=1,
        ),
        _report(
            task_index=1,
            collection_goal=tasks[1].collection_goal,
            evidence_count=2,
        ),
    ]
    first = _evidence(
        task_index=0,
        source_ref="external-0-0",
        url="https://example.com/shared",
    )
    duplicate = _evidence(
        task_index=1,
        source_ref="external-1-0",
        url="https://example.com/shared",
    )
    later_unique = _evidence(
        task_index=1,
        source_ref="external-1-1",
        url="https://example.com/unique",
    )
    runner = FakeExternalSearchRunner(
        _run_result(evidence=[first, duplicate, later_unique], task_reports=reports)
    )
    service = ExternalSearchService(runner=runner)

    outcome = await service.search(
        tasks,
        target_time_window="直近24時間",
        as_of=_as_of(),
    )

    assert outcome.evidence == [first, later_unique]
    assert outcome.deduplicated_evidence_count == 1
    assert [item.source_ref for item in outcome.evidence] == [
        "external-0-0",
        "external-1-1",
    ]


def test_outcome_rejects_task_report_count_mismatch() -> None:
    tasks = [_task("需要面を調べる"), _task("供給面を調べる")]

    with pytest.raises(ValidationError):
        external_search_module.ExternalSearchOutcome(
            tasks=tasks,
            evidence=[],
            task_reports=[
                _report(task_index=0, collection_goal=tasks[0].collection_goal),
            ],
            effective_agent_count=1,
        )


def test_outcome_rejects_duplicate_or_missing_report_task_indexes() -> None:
    tasks = [_task("需要面を調べる"), _task("供給面を調べる")]

    with pytest.raises(ValidationError):
        external_search_module.ExternalSearchOutcome(
            tasks=tasks,
            evidence=[],
            task_reports=[
                _report(task_index=0, collection_goal=tasks[0].collection_goal),
                _report(task_index=0, collection_goal=tasks[1].collection_goal),
            ],
            effective_agent_count=2,
        )

    with pytest.raises(ValidationError):
        external_search_module.ExternalSearchOutcome(
            tasks=tasks,
            evidence=[],
            task_reports=[
                _report(task_index=0, collection_goal=tasks[0].collection_goal),
                _report(task_index=2, collection_goal=tasks[1].collection_goal),
            ],
            effective_agent_count=2,
        )


def test_outcome_rejects_evidence_task_index_out_of_range() -> None:
    tasks = [_task("需要面を調べる")]

    with pytest.raises(ValidationError):
        external_search_module.ExternalSearchOutcome(
            tasks=tasks,
            evidence=[_unsafe_evidence(task_index=1)],
            task_reports=[
                _report(task_index=0, collection_goal=tasks[0].collection_goal),
            ],
            effective_agent_count=1,
        )


def test_outcome_rejects_duplicate_source_refs() -> None:
    tasks = [_task("需要面を調べる"), _task("供給面を調べる")]

    with pytest.raises(ValidationError):
        external_search_module.ExternalSearchOutcome(
            tasks=tasks,
            evidence=[
                _unsafe_evidence(task_index=0, source_ref="external-0-0"),
                _unsafe_evidence(task_index=1, source_ref="external-0-0"),
            ],
            task_reports=[
                _report(task_index=0, collection_goal=tasks[0].collection_goal),
                _report(task_index=1, collection_goal=tasks[1].collection_goal),
            ],
            effective_agent_count=2,
        )


def test_outcome_rejects_evidence_count_accounting_mismatch() -> None:
    tasks = [_task("需要面を調べる")]

    with pytest.raises(ValidationError):
        external_search_module.ExternalSearchOutcome(
            tasks=tasks,
            evidence=[_unsafe_evidence(task_index=0)],
            task_reports=[
                _report(
                    task_index=0,
                    collection_goal=tasks[0].collection_goal,
                    evidence_count=2,
                ),
            ],
            effective_agent_count=1,
        )
