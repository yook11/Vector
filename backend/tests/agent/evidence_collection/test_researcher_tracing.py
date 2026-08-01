"""Researcherの内部検索span契約。

正本仕様: ``specs/agent-phase-span-vocabulary-slice.md`` の
「工程とspanの対応」#4 (evidence_collection、内部検索)、Test contract
「新設したspan」。内部検索はAgentでなく機構が実行するため``agent_name``を
持たない。dedup / event等の振る舞い契約の正本は``test_researcher.py``であり、
ここはspan生成契約だけを持つ。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from logfire.testing import CaptureLogfire
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import StatusCode

from app.agent.evidence_collection import Researcher
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
    InternalSearchError,
)
from app.agent.planning.contract import ResearchTask
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.logfire._span_helpers import domain_attr_keys, exception_event, spans_named

_PHASE_SPAN_NAME = "agent_phase"
_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _task(*queries: str) -> ResearchTask:
    return ResearchTask(research_goal="goal", article_search_queries=list(queries))


def _hit(*, assessment_id: int, title: str) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=assessment_id - 1000,
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


class _InternalTool:
    def __init__(
        self,
        *,
        hits: list[InternalArticleSearchHit] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._hits = hits or []
        self._error = error

    @property
    def name(self) -> str:
        return "internal_search"

    async def invoke(self, input: object) -> list[InternalArticleSearchHit]:
        if self._error is not None:
            raise self._error
        return list(self._hits)


def _raw_phase_spans(capfire: CaptureLogfire) -> list:
    return [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _PHASE_SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]


async def test_internal_search_success_creates_span_scoped_to_task_without_agent_name(
    capfire: CaptureLogfire,
) -> None:
    researcher = Researcher(
        internal_search=_InternalTool(hits=[_hit(assessment_id=1001, title="a")])
    )

    await researcher.collect(
        task_index=3,
        task=_task("q"),
        external=None,
        date_filter=None,
        as_of=_AS_OF,
    )

    spans = spans_named(capfire, _PHASE_SPAN_NAME)
    assert len(spans) == 1
    span = spans[0]
    # 内部検索はAgentでなく機構が実行するため、agent_nameを持たない
    # (仕様Invariants「agent_nameはAgentが実行するspanにだけ付ける」)。
    assert domain_attr_keys(span["attributes"]) == {"phase", "task_index"}
    assert span["attributes"]["phase"] == "evidence_collection"
    assert span["attributes"]["task_index"] == 3
    assert exception_event(span) is None


async def test_internal_search_failure_marks_span_error_while_researcher_degrades(
    capfire: CaptureLogfire,
) -> None:
    """InternalSearchErrorはspanを未分類例外として貫通させたのち、

    researcherがspanの外で握ってdegrade(``return [], True``)へ変える。
    呼び出し側には例外が伝わらない。
    """
    researcher = Researcher(
        internal_search=_InternalTool(error=InternalSearchError(phase="article_search"))
    )

    collected = await researcher.collect(
        task_index=0,
        task=_task("q"),
        external=None,
        date_filter=None,
        as_of=_AS_OF,
    )

    assert (collected.internal_hits, collected.internal_failed) == ([], True)
    raw_spans = _raw_phase_spans(capfire)
    assert len(raw_spans) == 1
    assert raw_spans[0].status.status_code is StatusCode.ERROR
    span = spans_named(capfire, _PHASE_SPAN_NAME)[0]
    assert exception_event(span) is not None
    # 未分類扱いのため error.type は付かない (分類済み失敗との差)。
    assert ERROR_TYPE not in span["attributes"]


async def test_parallel_tasks_get_own_internal_search_span_with_distinct_task_index(
    capfire: CaptureLogfire,
) -> None:
    researcher = Researcher(internal_search=_InternalTool(hits=[]))

    await asyncio.gather(
        researcher.collect(
            task_index=0,
            task=_task("q0"),
            external=None,
            date_filter=None,
            as_of=_AS_OF,
        ),
        researcher.collect(
            task_index=1,
            task=_task("q1"),
            external=None,
            date_filter=None,
            as_of=_AS_OF,
        ),
    )

    spans = spans_named(capfire, _PHASE_SPAN_NAME)
    assert sorted(span["attributes"]["task_index"] for span in spans) == [0, 1]
