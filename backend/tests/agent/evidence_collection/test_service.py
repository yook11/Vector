"""EvidenceCollectionService.collect の1 goal分の収集契約テスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search import ExternalSearchService
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalSearch,
    ExternalSearchExecution,
    ExternalSearchHit,
    ExternalSearchProviderError,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
    InternalSearchError,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.planning.contract import ResearchTask, SearchPlan, TargetTimeWindow
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.running._harness import fixed_scope
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics

_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
_FUTURE_WINDOW = TargetTimeWindow(kind="calendar_month", year=2027, month=1)


def _task(goal: str, *queries: str) -> ResearchTask:
    return ResearchTask(
        research_goal=goal,
        article_search_queries=list(queries) or ["query"],
    )


def _plan(
    *tasks: ResearchTask,
    target_time_window: TargetTimeWindow | None = None,
) -> SearchPlan:
    return SearchPlan(
        research_tasks=list(tasks),
        target_time_window=target_time_window,
    )


def _hit(
    *,
    assessment_id: int,
    title: str,
    curation_id: int | None = None,
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id if curation_id is not None else assessment_id - 1000,
        title=title,
        summary=f"{title} summary",
        assessment_result=InScope(
            category=InScopeCategory.AI,
            investor_take="投資家視点",
            key_points=[],
        ),
    )
    return InternalArticleSearchHit(
        assessment_id=assessment_id,
        article=article,
        content=InternalArticleContent.from_article(article, published_at=None),
        distance=0.1,
    )


def _external_hit(url: str, *, title: str | None = None) -> ExternalSearchHit:
    return ExternalSearchHit(url=url, title=title or url, content="content")


def _query_draft(queries: list[str]) -> ExternalQueryDraft:
    return ExternalQueryDraft(queries=queries)


class _FakeInternalSearch:
    def __init__(
        self,
        *,
        hits: list[InternalArticleSearchHit] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._hits = hits or []
        self._error = error
        self.calls: list[InternalSearchQueries] = []

    async def search(self, queries: Any) -> list[InternalArticleSearchHit]:
        self.calls.append(queries)
        if self._error is not None:
            raise self._error
        return list(self._hits)


class _FakeExternalSearchGateway:
    def __init__(
        self,
        results_by_query: dict[str, list[ExternalSearchHit]] | None = None,
        *,
        errors_by_query: dict[str, BaseException] | None = None,
    ) -> None:
        self._results = results_by_query or {}
        self._errors = errors_by_query or {}
        self.calls: list[Any] = []

    async def search(self, request: Any) -> list[ExternalSearchHit]:
        self.calls.append(request)
        if request.query in self._errors:
            raise self._errors[request.query]
        return list(self._results.get(request.query, []))


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


class _Events:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


def _internal_events(events: list[Any]) -> list[Any]:
    return [
        event
        for event in events
        if event.type.startswith("evidence_collection.internal_search_")
    ]


def _external_events(events: list[Any]) -> list[Any]:
    return [
        event
        for event in events
        if event.type.startswith("evidence_collection.external_search_")
    ]


def _time_filter_metric_points(
    metrics: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    metric = next(
        (
            item
            for item in metrics
            if item["name"] == "external_search_time_filter_resolution_total"
        ),
        None,
    )
    if metric is None:
        return []
    return [
        (int(point["value"]), point.get("attributes", {}))
        for point in metric["data"]["data_points"]
    ]


def _external_search(
    query_runtime: object,
    *,
    gateway: _FakeExternalSearchGateway | None = None,
) -> ExternalSearch:
    return ExternalSearchService(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        search_gateway=(gateway or _FakeExternalSearchGateway()),  # type: ignore[arg-type]
    )


def _service(
    *,
    internal_search: Any,
    external: ExternalSearch | None = None,
    events: _Events | None = None,
    requested_agent_count: int | None = None,
) -> EvidenceCollectionService:
    return EvidenceCollectionService(
        internal_search=internal_search,
        events=events,
        external_search_scope_factory=fixed_scope(
            external if external is not None else _IdleExternalSearch()
        ),
        requested_agent_count=requested_agent_count,
    )


async def _collect_tasks(
    service: EvidenceCollectionService,
    *tasks: ResearchTask,
    target_time_window: TargetTimeWindow | None = None,
) -> list[Any]:
    collected = await service.collect(
        plan=_plan(*tasks, target_time_window=target_time_window),
        as_of=_AS_OF,
    )
    return collected.tasks


@pytest.mark.asyncio
async def test_internal_failure_still_collects_external_hits() -> None:
    """保証するテスト条件 1。internal_failed=Trueかつcompleted eventが立たない。"""
    events = _Events()
    gateway = _FakeExternalSearchGateway(
        {"nvidia supply": [_external_hit("https://example.com/a")]}
    )
    query_runtime = ScriptedAgentRuntime([_query_draft(["nvidia supply"])])
    service = _service(
        internal_search=_FakeInternalSearch(
            error=InternalSearchError(phase="article_search")
        ),
        external=_external_search(query_runtime, gateway=gateway),
        events=events,
    )

    [collected] = await _collect_tasks(service, _task("goal", "internal query"))

    assert (
        collected.internal_hits,
        collected.report.internal_collection,
        collected.report.external_collection,
        [str(hit.url) for hit in collected.external_hits],
        [event.type for event in _internal_events(events.events)],
    ) == (
        [],
        "failed",
        "succeeded",
        ["https://example.com/a"],
        ["evidence_collection.internal_search_started"],
    )


@pytest.mark.asyncio
async def test_external_provider_failure_keeps_internal_hits() -> None:
    """保証するテスト条件 2。"""
    hits = [_hit(assessment_id=1001, title="kept")]
    gateway = _FakeExternalSearchGateway(
        errors_by_query={
            "q": ExternalSearchProviderError(reason="external_search_http_error")
        }
    )
    query_runtime = ScriptedAgentRuntime([_query_draft(["q"])])
    service = _service(
        internal_search=_FakeInternalSearch(hits=hits),
        external=_external_search(query_runtime, gateway=gateway),
    )

    [collected] = await _collect_tasks(service, _task("goal", "internal query"))

    assert (
        [hit.content.title for hit in collected.internal_hits],
        collected.report.internal_collection,
        collected.report.external_collection,
        collected.external_hits,
        collected.report.provider_failed_query_count,
    ) == (["kept"], "succeeded", "provider_failed", [], 1)


@pytest.mark.asyncio
async def test_time_filter_failure_still_searches_without_date_filter(
    capfire: CaptureLogfire,
) -> None:
    """期間解決に失敗しても外部検索は行い、Tavilyへはdate_filterを渡さない。"""
    hits = [_hit(assessment_id=1001, title="only-internal")]
    gateway = _FakeExternalSearchGateway(
        {"nvidia supply": [_external_hit("https://example.com/a")]}
    )
    query_runtime = ScriptedAgentRuntime([_query_draft(["nvidia supply"])])
    service = _service(
        internal_search=_FakeInternalSearch(hits=hits),
        external=_external_search(query_runtime, gateway=gateway),
    )

    with capture_logs() as logs:
        [collected] = await _collect_tasks(
            service,
            _task("goal", "internal query"),
            target_time_window=_FUTURE_WINDOW,
        )
    metrics = collected_metrics(capfire)

    assert (
        [hit.content.title for hit in collected.internal_hits],
        collected.report.external_collection,
        [str(hit.url) for hit in collected.external_hits],
        [call.date_filter for call in gateway.calls],
        [call.input.target_time_window for call in query_runtime.calls],
        _time_filter_metric_points(metrics),
        [
            entry.get("reason")
            for entry in logs
            if entry.get("event") == "external_search_time_filter_failed"
        ],
    ) == (
        ["only-internal"],
        "succeeded",
        ["https://example.com/a"],
        [None],
        [_FUTURE_WINDOW],
        [(1, {"result": "failed", "reason": "future_calendar_month"})],
        ["future_calendar_month"],
    )


@pytest.mark.asyncio
async def test_time_filter_failure_records_one_metric_for_multiple_goals(
    capfire: CaptureLogfire,
) -> None:
    gateway = _FakeExternalSearchGateway(
        {
            "q1": [_external_hit("https://example.com/1")],
            "q2": [_external_hit("https://example.com/2")],
        }
    )
    query_runtime = ScriptedAgentRuntime([_query_draft(["q1"]), _query_draft(["q2"])])
    service = _service(
        internal_search=_FakeInternalSearch(),
        external=_external_search(query_runtime, gateway=gateway),
        requested_agent_count=1,
    )

    collected = await _collect_tasks(
        service,
        _task("first", "q1"),
        _task("second", "q2"),
        target_time_window=_FUTURE_WINDOW,
    )
    metrics = collected_metrics(capfire)

    assert (
        [task.report.external_collection for task in collected],
        [call.date_filter for call in gateway.calls],
        _time_filter_metric_points(metrics),
    ) == (
        ["succeeded", "succeeded"],
        [None, None],
        [(1, {"result": "failed", "reason": "future_calendar_month"})],
    )


@pytest.mark.asyncio
async def test_empty_queries_skip_gateway_even_when_time_filter_fails() -> None:
    gateway = _FakeExternalSearchGateway()
    query_runtime = ScriptedAgentRuntime(
        [AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)]
    )
    service = _service(
        internal_search=_FakeInternalSearch(),
        external=_external_search(query_runtime, gateway=gateway),
    )

    [collected] = await _collect_tasks(
        service,
        _task("goal", "internal query"),
        target_time_window=_FUTURE_WINDOW,
    )

    assert (
        collected.report.external_collection,
        collected.executed_queries,
        gateway.calls,
    ) == ("query_generation_failed", (), [])


@pytest.mark.asyncio
async def test_internal_search_receives_only_that_tasks_own_queries() -> None:
    """保証するテスト条件 4。"""
    internal_search = _FakeInternalSearch()
    service = _service(
        internal_search=internal_search,
        requested_agent_count=1,
    )

    await _collect_tasks(
        service,
        _task("first", "q1", "q2"),
        _task("second", "q3"),
    )

    assert internal_search.calls == [
        InternalSearchQueries(queries=("q1", "q2")),
        InternalSearchQueries(queries=("q3",)),
    ]


@pytest.mark.asyncio
async def test_independent_collect_calls_do_not_leak_failure_between_tasks() -> None:
    """保証するテスト条件 5。"""

    class _KeyedInternalSearch:
        async def search(self, queries: Any) -> list[InternalArticleSearchHit]:
            query = queries.queries[0]
            if query == "bad":
                raise InternalSearchError(phase="article_search")
            return [_hit(assessment_id=1001, title="sibling-hit")]

    class _KeyedExternalSearchGateway:
        async def search(self, request: Any) -> list[ExternalSearchHit]:
            if request.query == "bad":
                raise ExternalSearchProviderError(reason="external_search_http_error")
            return [_external_hit("https://example.com/good", title="good")]

    service = _service(
        internal_search=_KeyedInternalSearch(),
        external=_external_search(
            ScriptedAgentRuntime([_query_draft(["bad"]), _query_draft(["good"])]),
            gateway=_KeyedExternalSearchGateway(),
        ),
        requested_agent_count=1,
    )

    failing_result, sibling_result = await _collect_tasks(
        service,
        _task("failing", "bad"),
        _task("succeeding", "good"),
    )

    assert (
        failing_result.report.internal_collection,
        failing_result.report.external_collection,
        [hit.content.title for hit in sibling_result.internal_hits],
        [hit.title for hit in sibling_result.external_hits],
    ) == ("failed", "provider_failed", ["sibling-hit"], ["good"])


@pytest.mark.asyncio
async def test_internal_events_carry_task_index_and_input_derived_counts() -> None:
    """保証するテスト条件 10。"""
    events = _Events()
    hits_by_call = iter(
        [
            [_hit(assessment_id=1001, title="a"), _hit(assessment_id=1002, title="b")],
            [_hit(assessment_id=1003, title="c")],
        ]
    )

    class _PerCallInternalSearch:
        async def search(self, queries: Any) -> list[InternalArticleSearchHit]:
            return next(hits_by_call)

    service = _service(
        internal_search=_PerCallInternalSearch(),
        events=events,
        requested_agent_count=1,
    )

    await _collect_tasks(
        service,
        _task("first", "q1", "q2"),
        _task("second", "q3"),
    )

    internal_events = [event.model_dump() for event in _internal_events(events.events)]
    assert internal_events == [
        {
            "type": "evidence_collection.internal_search_started",
            "task_index": 0,
            "query_count": 2,
        },
        {
            "type": "evidence_collection.internal_search_completed",
            "task_index": 0,
            "hit_count": 2,
        },
        {
            "type": "evidence_collection.internal_search_started",
            "task_index": 1,
            "query_count": 1,
        },
        {
            "type": "evidence_collection.internal_search_completed",
            "task_index": 1,
            "hit_count": 1,
        },
    ]


@pytest.mark.asyncio
async def test_internal_failure_reports_started_only_with_task_index() -> None:
    """保証するテスト条件 11。"""
    events = _Events()
    service = _service(
        internal_search=_FakeInternalSearch(
            error=InternalSearchError(phase="query_embedding")
        ),
        events=events,
    )

    await _collect_tasks(
        service,
        _task("third", "q"),
    )

    internal_events = [event.model_dump() for event in _internal_events(events.events)]
    assert internal_events == [
        {
            "type": "evidence_collection.internal_search_started",
            "task_index": 0,
            "query_count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_external_events_fire_in_order_with_task_index_and_payload() -> None:
    """保証するテスト条件 12。"""
    events = _Events()
    gateway = _FakeExternalSearchGateway(
        {
            "good query": [
                _external_hit("https://example.com/x", title="x"),
                _external_hit("https://example.com/y", title="y"),
            ]
        }
    )
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["  good query  ", "good query"])]
    )
    service = _service(
        internal_search=_FakeInternalSearch(),
        external=_external_search(query_runtime, gateway=gateway),
        events=events,
    )

    await _collect_tasks(service, _task("goal", "internal query"))

    external_events = [event.model_dump() for event in _external_events(events.events)]
    assert external_events == [
        {
            "type": "evidence_collection.external_search_queries_generated",
            "task_index": 0,
            "queries": ["good query"],
        },
        {
            "type": "evidence_collection.external_search_hits_fetched",
            "task_index": 0,
            "hit_count": 2,
        },
    ]


@pytest.mark.asyncio
async def test_executed_queries_holds_generated_queries_in_order_on_success() -> None:
    """正本仕様: agent-research-checkpoint-context-slice.md「記録フロー」1。

    provider呼び出しが全件成功する場合、executed_queriesは生成queryの全件を
    生成順のまま保持する。
    """
    gateway = _FakeExternalSearchGateway(
        {
            "first query": [_external_hit("https://example.com/1")],
            "second query": [_external_hit("https://example.com/2")],
        }
    )
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["first query", "second query"])]
    )
    service = _service(
        internal_search=_FakeInternalSearch(),
        external=_external_search(query_runtime, gateway=gateway),
    )

    [collected] = await _collect_tasks(service, _task("goal", "internal query"))

    assert collected.executed_queries == ("first query", "second query")


@pytest.mark.asyncio
async def test_executed_queries_drops_only_the_failed_query_preserving_order() -> None:
    """executed_queriesはgenerated_queriesの部分列であり、生成順を保つ。

    3件中2件目のprovider呼び出しだけが失敗した場合、失敗したqueryだけが
    除かれ、残りの順序は生成順のまま変わらない。
    """
    gateway = _FakeExternalSearchGateway(
        {
            "q1": [_external_hit("https://example.com/1")],
            "q3": [_external_hit("https://example.com/3")],
        },
        errors_by_query={
            "q2": ExternalSearchProviderError(reason="external_search_http_error")
        },
    )
    query_runtime = ScriptedAgentRuntime([_query_draft(["q1", "q2", "q3"])])
    service = _service(
        internal_search=_FakeInternalSearch(),
        external=_external_search(query_runtime, gateway=gateway),
    )

    [collected] = await _collect_tasks(service, _task("goal", "internal query"))

    assert collected.report.generated_queries == ["q1", "q2", "q3"]
    assert collected.executed_queries == ("q1", "q3")
    assert collected.report.external_collection == "succeeded"


@pytest.mark.asyncio
async def test_executed_queries_is_empty_when_every_provider_call_fails() -> None:
    """全queryのprovider呼び出しが失敗する(external_collection=provider_failed)と

    記録できるqueryが0件になるため、executed_queriesは空tupleになる。
    """
    gateway = _FakeExternalSearchGateway(
        errors_by_query={
            "q1": ExternalSearchProviderError(reason="external_search_http_error"),
            "q2": ExternalSearchProviderError(reason="external_search_http_error"),
        }
    )
    query_runtime = ScriptedAgentRuntime([_query_draft(["q1", "q2"])])
    service = _service(
        internal_search=_FakeInternalSearch(),
        external=_external_search(query_runtime, gateway=gateway),
    )

    [collected] = await _collect_tasks(service, _task("goal", "internal query"))

    assert collected.report.external_collection == "provider_failed"
    assert collected.executed_queries == ()


@pytest.mark.asyncio
async def test_executed_queries_is_empty_when_query_generation_fails() -> None:
    """query生成自体が失敗するとexternal_collection=query_generation_failedになり、

    provider呼び出しが一度も起きないためexecuted_queriesは空tupleになる。
    """
    query_runtime = ScriptedAgentRuntime(
        [AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)]
    )
    service = _service(
        internal_search=_FakeInternalSearch(),
        external=_external_search(query_runtime),
    )

    [collected] = await _collect_tasks(service, _task("goal", "internal query"))

    assert collected.report.external_collection == "query_generation_failed"
    assert collected.executed_queries == ()
