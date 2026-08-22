"""Researcher(1 task分の内部/外部収集)の単体契約テスト。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.agent.evidence_collection import Researcher
from app.agent.evidence_collection.external_search import ExternalSearchService
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalResearchRuntime,
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
from app.agent.planning.contract import ResearchTask
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.runtime._fakes import ScriptedAgentRuntime

_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _task(goal: str, *queries: str) -> ResearchTask:
    return ResearchTask(
        research_goal=goal,
        article_search_queries=list(queries) or ["query"],
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


def _external_runtime(
    query_runtime: object,
    *,
    gateway: _FakeExternalSearchGateway | None = None,
) -> ExternalResearchRuntime:
    return ExternalResearchRuntime(
        external_search=ExternalSearchService(
            query_runtime=query_runtime,  # type: ignore[arg-type]
            search_gateway=(gateway or _FakeExternalSearchGateway()),  # type: ignore[arg-type]
        ),
        reviewer_runtime=ScriptedAgentRuntime([]),  # type: ignore[arg-type]
    )


async def _collect(
    researcher: Any,
    *,
    task_index: int = 0,
    task: ResearchTask | None = None,
    external: ExternalResearchRuntime | None = None,
    date_filter: object | None = None,
    as_of: datetime = _AS_OF,
) -> Any:
    return await researcher.collect(
        task_index=task_index,
        task=task or _task("task goal"),
        external=external,
        date_filter=date_filter,
        as_of=as_of,
    )


@pytest.mark.asyncio
async def test_internal_failure_still_collects_external_hits() -> None:
    """保証するテスト条件 1。internal_failed=Trueかつcompleted eventが立たない。"""
    events = _Events()
    gateway = _FakeExternalSearchGateway(
        {"nvidia supply": [_external_hit("https://example.com/a")]}
    )
    query_runtime = ScriptedAgentRuntime([_query_draft(["nvidia supply"])])
    researcher = Researcher(
        internal_search=_FakeInternalSearch(
            error=InternalSearchError(phase="article_search")
        ),
        events=events,
    )

    collected = await _collect(
        researcher,
        task=_task("goal", "internal query"),
        external=_external_runtime(query_runtime, gateway=gateway),
    )

    assert (
        collected.internal_hits,
        collected.internal_failed,
        collected.external_status,
        [str(hit.url) for hit in collected.external_hits],
        [event.type for event in _internal_events(events.events)],
    ) == (
        [],
        True,
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
    researcher = Researcher(internal_search=_FakeInternalSearch(hits=hits))

    collected = await _collect(
        researcher,
        task=_task("goal", "internal query"),
        external=_external_runtime(query_runtime, gateway=gateway),
    )

    assert (
        [hit.content.title for hit in collected.internal_hits],
        collected.internal_failed,
        collected.external_status,
        collected.external_hits,
        collected.provider_failed_query_count,
    ) == (["kept"], False, "provider_failed", [], 1)


@pytest.mark.asyncio
async def test_external_none_skips_external_collection_entirely() -> None:
    """保証するテスト条件 3(Researcher単体分)。"""
    hits = [_hit(assessment_id=1001, title="only-internal")]
    researcher = Researcher(internal_search=_FakeInternalSearch(hits=hits))

    collected = await _collect(researcher, task=_task("goal", "internal query"))

    assert (
        [hit.content.title for hit in collected.internal_hits],
        collected.external_status,
        collected.external_hits,
        collected.generated_queries,
        collected.provider_failed_query_count,
    ) == (["only-internal"], None, [], [], 0)


@pytest.mark.asyncio
async def test_internal_search_receives_only_that_tasks_own_queries() -> None:
    """保証するテスト条件 4。"""
    internal_search = _FakeInternalSearch()
    researcher = Researcher(internal_search=internal_search)

    await _collect(researcher, task_index=0, task=_task("first", "q1", "q2"))
    await _collect(researcher, task_index=1, task=_task("second", "q3"))

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

    internal_search = _KeyedInternalSearch()
    external_gateway = _KeyedExternalSearchGateway()
    researcher = Researcher(internal_search=internal_search)

    failing_result, sibling_result = await asyncio.gather(
        _collect(
            researcher,
            task_index=0,
            task=_task("failing", "bad"),
            external=_external_runtime(
                ScriptedAgentRuntime([_query_draft(["bad"])]), gateway=external_gateway
            ),
        ),
        _collect(
            researcher,
            task_index=1,
            task=_task("succeeding", "good"),
            external=_external_runtime(
                ScriptedAgentRuntime([_query_draft(["good"])]), gateway=external_gateway
            ),
        ),
    )

    assert (
        failing_result.internal_failed,
        failing_result.external_status,
        [hit.content.title for hit in sibling_result.internal_hits],
        [hit.title for hit in sibling_result.external_hits],
    ) == (True, "provider_failed", ["sibling-hit"], ["good"])


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

    researcher = Researcher(internal_search=_PerCallInternalSearch(), events=events)

    await _collect(researcher, task_index=0, task=_task("first", "q1", "q2"))
    await _collect(researcher, task_index=1, task=_task("second", "q3"))

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
    researcher = Researcher(
        internal_search=_FakeInternalSearch(
            error=InternalSearchError(phase="query_embedding")
        ),
        events=events,
    )

    await _collect(researcher, task_index=2, task=_task("third", "q"))

    internal_events = [event.model_dump() for event in _internal_events(events.events)]
    assert internal_events == [
        {
            "type": "evidence_collection.internal_search_started",
            "task_index": 2,
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
    researcher = Researcher(internal_search=_FakeInternalSearch(), events=events)

    await _collect(
        researcher,
        task_index=1,
        task=_task("goal", "internal query"),
        external=_external_runtime(query_runtime, gateway=gateway),
    )

    external_events = [event.model_dump() for event in _external_events(events.events)]
    assert external_events == [
        {
            "type": "evidence_collection.external_search_queries_generated",
            "task_index": 1,
            "queries": ["good query"],
        },
        {
            "type": "evidence_collection.external_search_hits_fetched",
            "task_index": 1,
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
    researcher = Researcher(internal_search=_FakeInternalSearch())

    collected = await _collect(
        researcher,
        task=_task("goal", "internal query"),
        external=_external_runtime(query_runtime, gateway=gateway),
    )

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
    researcher = Researcher(internal_search=_FakeInternalSearch())

    collected = await _collect(
        researcher,
        task=_task("goal", "internal query"),
        external=_external_runtime(query_runtime, gateway=gateway),
    )

    assert collected.generated_queries == ["q1", "q2", "q3"]
    assert collected.executed_queries == ("q1", "q3")
    assert collected.external_status == "succeeded"


@pytest.mark.asyncio
async def test_executed_queries_is_empty_when_every_provider_call_fails() -> None:
    """全queryのprovider呼び出しが失敗する(external_status=provider_failed)と

    記録できるqueryが0件になるため、executed_queriesは空tupleになる。
    """
    gateway = _FakeExternalSearchGateway(
        errors_by_query={
            "q1": ExternalSearchProviderError(reason="external_search_http_error"),
            "q2": ExternalSearchProviderError(reason="external_search_http_error"),
        }
    )
    query_runtime = ScriptedAgentRuntime([_query_draft(["q1", "q2"])])
    researcher = Researcher(internal_search=_FakeInternalSearch())

    collected = await _collect(
        researcher,
        task=_task("goal", "internal query"),
        external=_external_runtime(query_runtime, gateway=gateway),
    )

    assert collected.external_status == "provider_failed"
    assert collected.executed_queries == ()


@pytest.mark.asyncio
async def test_executed_queries_is_empty_when_query_generation_fails() -> None:
    """query生成自体が失敗するとexternal_status=query_generation_failedになり、

    provider呼び出しが一度も起きないためexecuted_queriesは空tupleになる。
    """
    query_runtime = ScriptedAgentRuntime(
        [AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)]
    )
    researcher = Researcher(internal_search=_FakeInternalSearch())

    collected = await _collect(
        researcher,
        task=_task("goal", "internal query"),
        external=_external_runtime(query_runtime),
    )

    assert collected.external_status == "query_generation_failed"
    assert collected.executed_queries == ()


@pytest.mark.asyncio
async def test_executed_queries_is_empty_when_external_runtime_is_none() -> None:
    """外部検索を実行しないtask(external=None)ではexecuted_queriesが空tupleになる。"""
    researcher = Researcher(internal_search=_FakeInternalSearch())

    collected = await _collect(researcher, task=_task("goal", "internal query"))

    assert collected.external_status is None
    assert collected.executed_queries == ()
