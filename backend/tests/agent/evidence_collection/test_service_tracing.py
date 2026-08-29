"""EvidenceCollectionのcollect/task/internal search span階層契約。"""

from __future__ import annotations

from datetime import UTC, datetime

from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search import ExternalSearchService
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalSearchExecution,
    ExternalSearchHit,
    ExternalSearchRequest,
)
from app.agent.evidence_collection.internal_search import (
    InternalSearchError,
    InternalSearchFailureCode,
    InternalSearchService,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.agent.planning.contract import ResearchTask, SearchPlan
from tests.agent.running._harness import fixed_scope
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
    spans_named,
)

_PHASE_SPAN_NAME = "agent_phase"
_TASK_SPAN_NAME = "evidence_collection_task"
_INTERNAL_SEARCH_SPAN_NAME = "internal_search"
_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _task(goal: str, *queries: str) -> ResearchTask:
    return ResearchTask(research_goal=goal, article_search_queries=list(queries))


def _plan(*tasks: ResearchTask) -> SearchPlan:
    return SearchPlan(research_tasks=list(tasks))


class _InternalQueryEmbedder:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self._error = error

    async def embed_queries(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalQueryEmbedding]:
        if self._error is not None:
            raise self._error
        return []


class _UnreachableArticleRepository:
    async def search_by_embedding(
        self,
        embedding: InternalQueryEmbedding,
        *,
        limit: int,
    ) -> list[object]:
        raise AssertionError(f"article search must not run: {embedding!r}, {limit}")


class _IdleExternalSearch:
    async def search(
        self,
        *,
        research_goal: str,
        as_of: object,
        target_time_window: object,
        task_index: int,
    ) -> ExternalSearchExecution:
        del research_goal, as_of, target_time_window, task_index
        return ExternalSearchExecution(
            generated_queries=(),
            hits=[],
            provider_failed_query_count=0,
            executed_queries=(),
        )


class _EmptyExternalGateway:
    async def search(self, request: ExternalSearchRequest) -> list[ExternalSearchHit]:
        del request
        return []


def _service(
    *,
    error: BaseException | None = None,
    external_search: object | None = None,
) -> EvidenceCollectionService:
    return EvidenceCollectionService(
        internal_search=InternalSearchService(
            embedder=_InternalQueryEmbedder(error=error),
            article_search_repository=_UnreachableArticleRepository(),  # type: ignore[arg-type]
        ),
        external_search_scope_factory=fixed_scope(
            external_search or _IdleExternalSearch()  # type: ignore[arg-type]
        ),
    )


def _raw_span(capfire: CaptureLogfire, name: str):
    spans = [
        span
        for span in capfire.exporter.exported_spans
        if span.name == name
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    assert len(spans) == 1
    return spans[0]


def _collection_span(capfire: CaptureLogfire):
    spans = [
        span
        for span in spans_named(capfire, _PHASE_SPAN_NAME)
        if span["attributes"].get("phase") == "evidence_collection"
        and "agent_name" not in span["attributes"]
    ]
    assert len(spans) == 1
    return spans[0]


async def test_success_creates_collect_task_and_internal_search_hierarchy(
    capfire: CaptureLogfire,
) -> None:
    collected = await _service().collect(
        plan=_plan(_task("goal", "q")),
        as_of=_AS_OF,
    )

    assert collected.tasks[0].report.internal_collection == "succeeded"
    collection_span = _collection_span(capfire)
    task_span = one_span_named(capfire, _TASK_SPAN_NAME)
    internal_span = one_span_named(capfire, _INTERNAL_SEARCH_SPAN_NAME)
    assert domain_attr_keys(collection_span["attributes"]) == {"phase"}
    assert collection_span["attributes"]["phase"] == "evidence_collection"
    assert domain_attr_keys(task_span["attributes"]) == {"task_index"}
    assert task_span["attributes"]["task_index"] == 0
    assert domain_attr_keys(internal_span["attributes"]) == set()
    assert task_span["parent"]["span_id"] == collection_span["context"]["span_id"]
    assert internal_span["parent"]["span_id"] == task_span["context"]["span_id"]
    assert all(
        exception_event(span) is None
        for span in (collection_span, task_span, internal_span)
    )


async def test_internal_failure_marks_only_internal_search_error_and_degrades(
    capfire: CaptureLogfire,
) -> None:
    error = InternalSearchError(
        code=InternalSearchFailureCode.QUERY_EMBEDDING_FAILED
    )

    collected = await _service(error=error).collect(
        plan=_plan(_task("goal", "q")),
        as_of=_AS_OF,
    )

    assert collected.tasks[0].report.internal_collection == "failed"
    collection_span = _collection_span(capfire)
    task_span = one_span_named(capfire, _TASK_SPAN_NAME)
    internal_span = one_span_named(capfire, _INTERNAL_SEARCH_SPAN_NAME)
    assert exception_event(internal_span) is not None
    assert exception_event(task_span) is None
    assert exception_event(collection_span) is None
    assert (
        _raw_span(capfire, _INTERNAL_SEARCH_SPAN_NAME).status.status_code
        is StatusCode.ERROR
    )
    assert _raw_span(capfire, _TASK_SPAN_NAME).status.status_code is StatusCode.UNSET
    assert _raw_span(capfire, _PHASE_SPAN_NAME).status.status_code is StatusCode.UNSET


async def test_parallel_tasks_get_distinct_task_and_internal_search_spans(
    capfire: CaptureLogfire,
) -> None:
    external_search = ExternalSearchService(
        query_runtime=ScriptedAgentRuntime(
            [
                ExternalQueryDraft(queries=["q0"]),
                ExternalQueryDraft(queries=["q1"]),
            ]
        ),
        search_gateway=_EmptyExternalGateway(),
    )
    collected = await _service(external_search=external_search).collect(
        plan=_plan(_task("goal-0", "q0"), _task("goal-1", "q1")),
        as_of=_AS_OF,
    )

    assert [task.task_index for task in collected.tasks] == [0, 1]
    collection_span = _collection_span(capfire)
    task_spans = spans_named(capfire, _TASK_SPAN_NAME)
    internal_spans = spans_named(capfire, _INTERNAL_SEARCH_SPAN_NAME)
    external_spans = spans_named(capfire, "external_search")
    query_spans = [
        span
        for span in spans_named(capfire, _PHASE_SPAN_NAME)
        if "agent_name" in span["attributes"]
    ]
    assert {span["attributes"]["task_index"] for span in task_spans} == {0, 1}
    assert len(internal_spans) == 2
    assert len(external_spans) == 2
    assert len(query_spans) == 2
    assert all(
        task["parent"]["span_id"] == collection_span["context"]["span_id"]
        for task in task_spans
    )
    assert {span["parent"]["span_id"] for span in internal_spans} == {
        span["context"]["span_id"] for span in task_spans
    }
    assert {span["parent"]["span_id"] for span in external_spans} == {
        span["context"]["span_id"] for span in task_spans
    }
    assert {span["parent"]["span_id"] for span in query_spans} == {
        span["context"]["span_id"] for span in external_spans
    }
