"""AnsweringRunner 所有の external phase trace 契約。"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from openai import AsyncOpenAI
from opentelemetry.trace import StatusCode

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.evidence_collection import NewsCollector, Researcher
from app.agent.evidence_collection.external_search.agent import EXTERNAL_QUERY_AGENT
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
    ExternalSearchHit,
    ExternalSearchToolInput,
)
from app.agent.evidence_collection.external_search.deepseek_binding import (
    EXTERNAL_QUERY_DEEPSEEK_BINDING,
)
from app.agent.evidence_collection.internal_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
    InternalSearchError,
)
from app.agent.evidence_review import EvidenceReviewer
from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.evidence_review.deepseek_binding import (
    EVIDENCE_REVIEWER_DEEPSEEK_BINDING,
)
from app.agent.planning.contract import (
    DirectAnswerPlan,
    PlanningRequest,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.question_context import AnswerBrief
from app.agent.running import AnsweringPhases, AnsweringRunner, RunInput
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.agent.runtime.deepseek import DeepSeekAgentRuntime
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from app.logfire.redaction import install_exception_redaction
from tests.agent.running._harness import run_identity
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._deepseek_helpers import FakeDeepSeekClient, function_response
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
    spans_named,
)

_PHASE_SPAN_NAME = "agent_phase"
_PROVIDER_SPAN_NAME = "agent_provider_call"
_RUN_SPAN_NAME = "agent_answering_run"
_QUERY_OUTPUT_SENTINEL = "GENERATED_QUERY_SENTINEL_1f24"
_SELECTION_CLAIM_SENTINEL = "SELECTION_CLAIM_SENTINEL_98ab"
_SELECTION_WHY_SENTINEL = "SELECTION_WHY_SENTINEL_7c31"


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )


def _query_response() -> object:
    return function_response(
        function_name=EXTERNAL_QUERY_DEEPSEEK_BINDING.function_name,
        arguments=json.dumps({"queries": [_QUERY_OUTPUT_SENTINEL]}),
        usage=_usage(),
    )


def _reviewer_response(*, option_indexes: list[int] | None = None) -> object:
    """D4-S1: 内外統合index空間で選ぶoption_indexを呼び出し側が指定する。

    既定の[0]は、internalヒットが空(_EmptyInternalSearch)のtaskで唯一の外部の
    選択肢(index 0)を選ぶ後方互換値。internalヒットがあるtaskは呼び出し側が明示する。
    """
    return function_response(
        function_name=EVIDENCE_REVIEWER_DEEPSEEK_BINDING.function_name,
        arguments=json.dumps(
            {
                "selections": [
                    {
                        "option_index": index,
                        "claim": f"{_SELECTION_CLAIM_SENTINEL}_{index}",
                        "why_selected": _SELECTION_WHY_SENTINEL,
                    }
                    for index in (option_indexes or [0])
                ],
                "missing": [],
            }
        ),
        usage=_usage(),
    )


class _Preparer:
    async def prepare(self, **_kwargs: object) -> AnswerBrief:
        return AnswerBrief(standalone_question="NVIDIA の見通しは？")


class _Planner:
    async def plan(self, request: PlanningRequest) -> Any:
        del request
        return SearchPlan(
            research_tasks=[
                ResearchTask(
                    research_goal="GOAL_SENTINEL_3cc7",
                    article_search_queries=["NVIDIA の直近発表"],
                )
            ],
            target_time_window=TargetTimeWindow(
                kind="calendar_month",
                year=1998,
                month=2,
            ),
        )


class _EmptyInternalSearch:
    async def search(self, queries: object) -> list[object]:
        del queries
        return []


class _UnreachableDirectAnswerer:
    async def answer(
        self, *, request: AnsweringRequest, previous_answer: str = ""
    ) -> DirectAnswerDraft:
        raise AssertionError(
            f"direct answer must not run: {request!r} {previous_answer!r}"
        )


class _EvidenceAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[Any],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerDraft:
        del request, target_time_window, review_missing
        return EvidenceAnswerDraft(
            answer="根拠に基づく回答です。",
            cited_refs=[item.source.source_ref for item in evidence],
        )


class _Tool:
    def __init__(self) -> None:
        self.inputs: list[ExternalSearchToolInput] = []

    @property
    def name(self) -> str:
        return "external_search"

    async def search(self, input: ExternalSearchToolInput) -> list[ExternalSearchHit]:
        self.inputs.append(input)
        return [
            ExternalSearchHit(
                url="https://example.com/TRACE_URL_SENTINEL_63df",
                title="HIT_TITLE_SENTINEL_4cab",
                content="HIT_SNIPPET_SENTINEL_00f4",
                source_name="Example",
            )
        ]


class _Scope(AbstractAsyncContextManager[ExternalResearchRuntime]):
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime

    async def __aenter__(self) -> ExternalResearchRuntime:
        return self._runtime

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, exc, traceback
        return False


class _Factory:
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime

    def activate(self) -> _Scope:
        return _Scope(self._runtime)


def _runner(
    *,
    query_client: FakeDeepSeekClient,
    reviewer_client: FakeDeepSeekClient,
    search_tool: _Tool | None = None,
    internal_search: object | None = None,
    evidence_answerer: object | None = None,
) -> tuple[AnsweringRunner, _Tool]:
    tool = search_tool or _Tool()
    runtime = ExternalResearchRuntime(
        query_runtime=DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, query_client),
            binding=EXTERNAL_QUERY_DEEPSEEK_BINDING,
        ),
        reviewer_runtime=DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, reviewer_client),
            binding=EVIDENCE_REVIEWER_DEEPSEEK_BINDING,
        ),
        search_tool=tool,
    )
    phases = AnsweringPhases(
        planner=_Planner(),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=internal_search or _EmptyInternalSearch()
            )
        ),
        external_runtime_factory=_Factory(runtime),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=evidence_answerer or _EvidenceAnswerer(),
        reviewer=EvidenceReviewer(),
    )
    return (
        AnsweringRunner(
            input_safety_checker=AllowInputSafetyChecker(),
            context_preparer=_Preparer(),
            phases_factory=lambda: phases,
        ),
        tool,
    )


async def _run(runner: AnsweringRunner) -> None:
    await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        identity=run_identity(
            run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197652"),
            as_of=datetime(2026, 7, 19, tzinfo=UTC),
        ),
    )


@pytest.mark.asyncio
async def test_external_phase_spans_keep_attributes_parentage_and_no_sensitive_trace(
    capfire: CaptureLogfire,
) -> None:
    raw_selector_response_sentinel = "RAW_SELECTOR_RESPONSE_SENTINEL_5d71"
    query_client = FakeDeepSeekClient([_query_response()])
    reviewer_client = FakeDeepSeekClient(
        [
            function_response(
                function_name=EVIDENCE_REVIEWER_DEEPSEEK_BINDING.function_name,
                arguments=raw_selector_response_sentinel,
                usage=_usage(),
            ),
            _reviewer_response(),
        ]
    )
    runner, tool = _runner(
        query_client=query_client,
        reviewer_client=reviewer_client,
    )

    await _run(runner)

    phases = spans_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    # phase spanはAgent所有2本(external_query / evidence_reviewer、agent_name属性あり)
    # + 機構所有1本(内部検索、agent_nameを持たない)。内部検索spanの属性契約自体の
    # 正本は tests/agent/evidence_collection/test_researcher_tracing.py が持つため、
    # ここでは「同じevidence_collection系統のspanとしてtrace上に並ぶ」ことだけを見る。
    agent_phases = [
        phase
        for phase in phases
        if "agent_name" in domain_attr_keys(phase["attributes"])
    ]
    internal_search_phases = [
        phase
        for phase in phases
        if "agent_name" not in domain_attr_keys(phase["attributes"])
    ]
    phase_by_agent = {
        phase["attributes"]["agent_name"]: phase for phase in agent_phases
    }
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(), ensure_ascii=False, default=str
    )
    assert query_client.chat.completions.create.await_count == 1
    assert reviewer_client.chat.completions.create.await_count == 2
    assert [input.query for input in tool.inputs] == [_QUERY_OUTPUT_SENTINEL]
    assert len(phases) == 3
    assert len(providers) == 3
    assert set(phase_by_agent) == {
        EXTERNAL_QUERY_AGENT.name,
        EVIDENCE_REVIEWER_AGENT.name,
    }
    assert len(internal_search_phases) == 1
    assert domain_attr_keys(internal_search_phases[0]["attributes"]) == {
        "phase",
        "task_index",
    }
    assert internal_search_phases[0]["attributes"]["phase"] == "evidence_collection"
    # S1: evidence_review のphase spanはRun全体を覆うため task_index を持たない
    # (仕様「観測と失敗分類」)。task単位のexternal_query phaseは維持する。
    assert domain_attr_keys(
        phase_by_agent[EXTERNAL_QUERY_AGENT.name]["attributes"]
    ) == {"phase", "agent_name", "task_index"}
    assert phase_by_agent[EXTERNAL_QUERY_AGENT.name]["attributes"]["task_index"] == 0
    assert domain_attr_keys(
        phase_by_agent[EVIDENCE_REVIEWER_AGENT.name]["attributes"]
    ) == {"phase", "agent_name"}
    # 外部クエリ生成とevidence reviewは同じevidence_collection/evidence_review系統内で
    # 工程名が別れる (specs/agent-phase-span-vocabulary-slice.md 工程とspanの対応)。
    assert (
        phase_by_agent[EXTERNAL_QUERY_AGENT.name]["attributes"]["phase"]
        == "evidence_collection"
    )
    assert (
        phase_by_agent[EVIDENCE_REVIEWER_AGENT.name]["attributes"]["phase"]
        == "evidence_review"
    )
    assert all("task_index" not in provider["attributes"] for provider in providers)
    assert [provider["attributes"]["attempt_number"] for provider in providers] == [
        1,
        1,
        2,
    ]
    assert [provider["attributes"]["result"] for provider in providers] == [
        "succeeded",
        "invalid_response",
        "succeeded",
    ]
    assert (
        providers[0]["parent"]["span_id"]
        == phase_by_agent[EXTERNAL_QUERY_AGENT.name]["context"]["span_id"]
    )
    assert all(
        provider["parent"]["span_id"]
        == phase_by_agent[EVIDENCE_REVIEWER_AGENT.name]["context"]["span_id"]
        for provider in providers[1:]
    )
    assert all(exception_event(phase) is None for phase in phases)
    for unsafe in (
        "GOAL_SENTINEL_3cc7",
        "1998年2月",
        "TRACE_URL_SENTINEL_63df",
        "HIT_TITLE_SENTINEL_4cab",
        "HIT_SNIPPET_SENTINEL_00f4",
        raw_selector_response_sentinel,
        _QUERY_OUTPUT_SENTINEL,
        _SELECTION_CLAIM_SENTINEL,
        _SELECTION_WHY_SENTINEL,
    ):
        assert unsafe not in trace_dump


@pytest.mark.asyncio
async def test_unclassified_query_error_is_redacted_and_only_error_phase(
    capfire: CaptureLogfire,
) -> None:
    install_exception_redaction()
    error_sentinel = "UNCLASSIFIED_QUERY_ERROR_SENTINEL_4ea2"
    error = RuntimeError(error_sentinel)
    query_client = FakeDeepSeekClient([error])
    reviewer_client = FakeDeepSeekClient([_reviewer_response()])
    runner, _ = _runner(
        query_client=query_client,
        reviewer_client=reviewer_client,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

    phases = spans_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    # 内部検索は外部クエリ生成と並行実行され、外部クエリの未分類エラーとは無関係に
    # 成功で完走する(Researcher._gather_two_branchesが両枝をsettleさせてから
    # 例外を再送出するため)。「エラーになるphaseはひとつだけ」の意図を保つため、
    # phaseをexception eventの有無で分けて検証する。
    error_phases = [phase for phase in phases if exception_event(phase) is not None]
    healthy_phases = [phase for phase in phases if exception_event(phase) is None]
    raw_spans_by_id = {
        span.context.span_id: span
        for span in capfire.exporter.exported_spans
        if span.name in {_PHASE_SPAN_NAME, _PROVIDER_SPAN_NAME}
        and (span.attributes or {}).get("logfire.span_type") == "span"
    }
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(), ensure_ascii=False, default=str
    )
    assert raised.value is error
    assert len(phases) == 2
    assert len(providers) == 1
    assert len(error_phases) == 1
    assert len(healthy_phases) == 1
    assert "agent_name" not in domain_attr_keys(healthy_phases[0]["attributes"])
    assert providers[0]["parent"]["span_id"] == error_phases[0]["context"]["span_id"]
    assert reviewer_client.chat.completions.create.await_count == 0
    error_raw_spans = [
        raw_spans_by_id[phase["context"]["span_id"]] for phase in error_phases
    ] + [raw_spans_by_id[provider["context"]["span_id"]] for provider in providers]
    healthy_raw_spans = [
        raw_spans_by_id[phase["context"]["span_id"]] for phase in healthy_phases
    ]
    assert all(
        exception_event(span) is not None for span in [*error_phases, *providers]
    )
    assert all(exception_event(span) is None for span in healthy_phases)
    assert all(span.status.status_code is StatusCode.ERROR for span in error_raw_spans)
    assert all(span.status.description == "[redacted]" for span in error_raw_spans)
    assert all(
        span.status.status_code is StatusCode.UNSET for span in healthy_raw_spans
    )
    assert error_sentinel not in trace_dump


def _internal_hit(
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


class _TwoInternalHitsSearch:
    """span 採用数テスト用に、curation_id の異なる internal hit を2件返す。"""

    async def search(self, queries: object) -> list[InternalArticleSearchHit]:
        del queries
        return [
            _internal_hit(assessment_id=2001, title="INTERNAL_HIT_TITLE_SENTINEL_bf12"),
            _internal_hit(assessment_id=2002, title="INTERNAL_HIT_TITLE_SENTINEL_9a04"),
        ]


class _SelectiveEvidenceAnswerer:
    """cited_refs を evidence 入力から選ぶ関数で決める、
    span採用数テスト用の draft answerer。
    """

    def __init__(self, select: Callable[[list[Any]], list[str]]) -> None:
        self._select = select

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[Any],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerDraft:
        del request, target_time_window, review_missing
        return EvidenceAnswerDraft(
            answer="根拠に基づく回答です。",
            cited_refs=self._select(evidence),
        )


class _DirectPlanner:
    async def plan(self, request: PlanningRequest) -> Any:
        del request
        return DirectAnswerPlan()


class _DirectAnswerer:
    async def answer(
        self, *, request: AnsweringRequest, previous_answer: str = ""
    ) -> DirectAnswerDraft:
        del request, previous_answer
        return DirectAnswerDraft(answer="直接回答です。")


class _UnreachableEvidenceAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[Any],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerDraft:
        raise AssertionError(
            f"evidence answerer must not run on direct path: {request!r} "
            f"{evidence!r} {target_time_window!r} {review_missing!r}"
        )


class _UnreachableExternalFactory:
    def activate(self) -> AbstractAsyncContextManager[ExternalResearchRuntime]:
        raise AssertionError("external runtime scope must not activate on direct path")


class _PerQueryInternalHitsSearch:
    """queryごとに固定hitを返す、内部合流dedup統計テスト用のfake。"""

    def __init__(
        self, hits_by_query: dict[str, list[InternalArticleSearchHit]]
    ) -> None:
        self._hits_by_query = hits_by_query

    async def search(self, queries: Any) -> list[InternalArticleSearchHit]:
        query = queries.queries[0]
        return list(self._hits_by_query[query])


class _PerQueryFailableInternalSearch:
    """queryごとに成功/InternalSearchError失敗を切り替える、失敗task数テスト用fake。"""

    def __init__(self, *, failing_queries: set[str]) -> None:
        self._failing_queries = failing_queries
        self._next_assessment_id = 5001

    async def search(self, queries: Any) -> list[InternalArticleSearchHit]:
        query = queries.queries[0]
        if query in self._failing_queries:
            raise InternalSearchError(phase="article_search")
        hit = _internal_hit(assessment_id=self._next_assessment_id, title=query)
        self._next_assessment_id += 1
        return [hit]


class _TwoTaskPlanner:
    def __init__(self, plan: SearchPlan) -> None:
        self._plan = plan

    async def plan(self, request: PlanningRequest) -> Any:
        del request
        return self._plan


def _two_task_plan(*, task_queries: tuple[list[str], list[str]]) -> SearchPlan:
    return SearchPlan(
        research_tasks=[
            ResearchTask(research_goal=f"goal-{index}", article_search_queries=queries)
            for index, queries in enumerate(task_queries)
        ],
        target_time_window=TargetTimeWindow(kind="calendar_month", year=1998, month=2),
    )


def _review_draft_selecting_all_offered_options() -> Any:
    """S1: 統合index空間の選択肢(最大4件、2 task×内部2件)を全て採用するdraft。

    reviewerはRun単位1回のため、この1つのdraftだけで両taskの選択肢が
    Run全体の通しindexで選ばれる必要がある(旧: taskごとに(0,1)を別々の
    callで選ぶ前提だったが、Run単位1回ではそれだと2個目のtaskの選択肢に
    通しindexで到達できない)。選択肢がそれより少ないtask構成では超過indexが
    範囲外dropとなるだけで安全に使い回せる
    (このモジュールの複数taskにまたがる内部統計テスト専用の軽量fake)。
    """
    from app.agent.evidence_review import EvidenceReviewerDraft

    return EvidenceReviewerDraft.model_validate(
        {
            "selections": [
                {
                    "option_index": index,
                    "claim": f"claim-{index}",
                    "why_selected": "w",
                }
                for index in range(4)
            ],
            "missing": [],
        }
    )


def _two_task_query_failing_runtime() -> ExternalResearchRuntime:
    """外部query生成を2 task分とも失敗させ、外部ヒットを空に保つ(内部統計だけに絞る)。

    D4-S1: internalヒットが非空ならヒットゼロtaskではないためreviewerが起動する。
    S1: reviewerはRun単位1回のため script は1件だけ渡す (2回目の呼び出しは
    枯渇の AssertionError で即 red になる)。
    """
    query_failure = AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)
    return ExternalResearchRuntime(
        query_runtime=ScriptedAgentRuntime([query_failure, query_failure]),
        reviewer_runtime=ScriptedAgentRuntime(
            [_review_draft_selecting_all_offered_options()]
        ),
        search_tool=_Tool(),
    )


@pytest.mark.asyncio
async def test_evidence_run_span_reports_internal_external_counts_and_citations(
    capfire: CaptureLogfire,
) -> None:
    """内部hit2件・外部evidence1件のうち内部1・外部1が引用された場合の採用数/引用数を検証する。

    D4-S1: internal_evidence_countはreviewerが精査採用した件数になるため、
    統合index空間(内部0,1・外部2)の全選択肢を採用するreviewer応答を使う。
    """
    query_client = FakeDeepSeekClient([_query_response()])
    reviewer_client = FakeDeepSeekClient([_reviewer_response(option_indexes=[0, 1, 2])])
    runner, _ = _runner(
        query_client=query_client,
        reviewer_client=reviewer_client,
        internal_search=_TwoInternalHitsSearch(),
        evidence_answerer=_SelectiveEvidenceAnswerer(
            lambda evidence: [
                evidence[0].source.source_ref,
                evidence[-1].source.source_ref,
            ]
        ),
    )

    await _run(runner)

    attributes = one_span_named(capfire, _RUN_SPAN_NAME)["attributes"]
    attributes_dump = json.dumps(attributes, ensure_ascii=False, default=str)
    assert (
        attributes.get("internal_evidence_count"),
        attributes.get("external_evidence_count"),
        attributes.get("internal_cited_count"),
        attributes.get("external_cited_count"),
    ) == (2, 1, 1, 1)
    for unsafe in (
        "INTERNAL_HIT_TITLE_SENTINEL_bf12",
        "INTERNAL_HIT_TITLE_SENTINEL_9a04",
        "TRACE_URL_SENTINEL_63df",
        "HIT_TITLE_SENTINEL_4cab",
    ):
        assert unsafe not in attributes_dump


@pytest.mark.asyncio
async def test_direct_path_run_span_has_no_evidence_count_attributes(
    capfire: CaptureLogfire,
) -> None:
    """direct path では内部・外部の採用数・引用数属性が付かない。"""
    phases = AnsweringPhases(
        planner=_DirectPlanner(),
        collector=NewsCollector(
            researcher=Researcher(internal_search=_EmptyInternalSearch())
        ),
        external_runtime_factory=_UnreachableExternalFactory(),
        direct_answerer=_DirectAnswerer(),
        evidence_answerer=_UnreachableEvidenceAnswerer(),
        reviewer=EvidenceReviewer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )

    await _run(runner)

    attributes = one_span_named(capfire, _RUN_SPAN_NAME)["attributes"]
    assert domain_attr_keys(attributes).isdisjoint(
        {
            "internal_evidence_count",
            "external_evidence_count",
            "internal_cited_count",
            "external_cited_count",
            "internal_deduplicated_count",
            "internal_collection_failed_task_count",
        }
    )


@pytest.mark.asyncio
async def test_evidence_run_span_marks_zero_cited_explicitly_when_uncited(
    capfire: CaptureLogfire,
) -> None:
    """内部hitが1件も引用されない境界で、
    internal_cited_count が不在ではなく明示的な0で付く。
    """
    query_client = FakeDeepSeekClient([_query_response()])
    reviewer_client = FakeDeepSeekClient([_reviewer_response(option_indexes=[0, 1, 2])])
    runner, _ = _runner(
        query_client=query_client,
        reviewer_client=reviewer_client,
        internal_search=_TwoInternalHitsSearch(),
        evidence_answerer=_SelectiveEvidenceAnswerer(
            lambda evidence: [evidence[-1].source.source_ref]
        ),
    )

    await _run(runner)

    attributes = one_span_named(capfire, _RUN_SPAN_NAME)["attributes"]
    assert "internal_cited_count" in attributes
    assert (
        attributes.get("internal_evidence_count"),
        attributes.get("internal_cited_count"),
        attributes.get("external_evidence_count"),
        attributes.get("external_cited_count"),
    ) == (2, 0, 1, 1)


@pytest.mark.asyncio
async def test_evidence_run_span_reports_post_dedup_internal_total(
    capfire: CaptureLogfire,
) -> None:
    """同じ内部検索の記事でもtaskが異なれば、採用数は選択肢件数のまま残る。"""
    hits_by_query = {
        "task0 query": [
            _internal_hit(assessment_id=3001, title="task0-shared", curation_id=4200),
            _internal_hit(assessment_id=3002, title="task0-unique", curation_id=4201),
        ],
        "task1 query": [
            _internal_hit(
                assessment_id=3003, title="task1-shared-dup", curation_id=4200
            ),
            _internal_hit(assessment_id=3004, title="task1-unique", curation_id=4202),
        ],
    }
    phases = AnsweringPhases(
        planner=_TwoTaskPlanner(
            _two_task_plan(task_queries=(["task0 query"], ["task1 query"]))
        ),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=_PerQueryInternalHitsSearch(hits_by_query)
            )
        ),
        external_runtime_factory=_Factory(_two_task_query_failing_runtime()),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=_EvidenceAnswerer(),
        reviewer=EvidenceReviewer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )

    await _run(runner)

    offered_internal_count = sum(len(hits) for hits in hits_by_query.values())
    attributes = one_span_named(capfire, _RUN_SPAN_NAME)["attributes"]
    assert attributes.get("internal_evidence_count") == offered_internal_count
    assert "internal_deduplicated_count" not in attributes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_queries",
    [
        pytest.param({"task0 query"}, id="one-of-two-tasks-fails"),
        pytest.param(set(), id="all-tasks-succeed"),
    ],
)
async def test_evidence_run_span_reports_internal_collection_failed_task_count(
    capfire: CaptureLogfire,
    failing_queries: set[str],
) -> None:
    """内部収集が失敗したtask数がinternal_collection_failed_task_countに一致し、
    全task成功時は0になる。
    """
    task_queries = ("task0 query", "task1 query")
    phases = AnsweringPhases(
        planner=_TwoTaskPlanner(
            _two_task_plan(
                task_queries=([task_queries[0]], [task_queries[1]]),
            )
        ),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=_PerQueryFailableInternalSearch(
                    failing_queries=failing_queries
                )
            )
        ),
        external_runtime_factory=_Factory(_two_task_query_failing_runtime()),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=_EvidenceAnswerer(),
        reviewer=EvidenceReviewer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )

    await _run(runner)

    expected_failed_task_count = len(failing_queries & set(task_queries))
    attributes = one_span_named(capfire, _RUN_SPAN_NAME)["attributes"]
    assert (
        attributes.get("internal_collection_failed_task_count")
        == expected_failed_task_count
    )
