"""AnsweringRunner: Evidence Reviewer をRun単位1回へ広げる契約(S1)。

backend/specs/evidence-review-run-scope-slice.md の Invariants / Test contract を
`AnsweringRunner.run()` の黒箱から検証する。production はまだtask単位のまま
のため、大半はredになる想定(パケットの「期待するredの結果と理由」を参照)。

新しいグループ化candidate入力・Run全体の通し番号indexは、spec上の field名が
固定されていないため、この正本テストは以下の安定境界だけを覗く:
- reviewer呼び出し回数と入力(`ScriptedAgentRuntime.calls`)
- 入力の可視文字列化(`EVIDENCE_REVIEWER_AGENT.prompt.input_renderer`。
  production未変更のAgent宣言契約が持つ安定した拡張点)
- reviewerの出力型(`EvidenceReviewDraft`。仕様上変更されない応答契約)
- `AnsweringRunner.run()` の最終出力・event(仕様上変更されないSSE/response契約)

複数taskにまたがるcandidate_indexを使うテストでは、
「research_goalごとにグループ化し、グループ内は内部候補が先・外部候補が後、
グループはtask_index昇順で結合する」という仕様の唯一自然な解釈から
Run全体の通し番号を入力fixtureのcandidate件数から導出する。
現行のtask単位呼び出しでも例外にならないよう、余剰callへは空draftを充て
(`_padded_script`)、意味のある不一致だけがredになるようにしている。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.evidence_collection import Researcher
from app.agent.evidence_collection.evidence_review import (
    EvidenceReviewDraft,
    EvidenceReviewer,
)
from app.agent.evidence_collection.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalResearchRuntime,
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
    InternalSearchError,
)
from app.agent.planning.contract import ResearchTask, SearchPlan, TargetTimeWindow
from app.agent.question_context import (
    AnswerRequirement,
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextTelemetry,
)
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197661")
AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)

_EMPTY_DRAFT = EvidenceReviewDraft.model_validate({"selections": [], "missing": []})


def _draft(
    selections: list[dict[str, Any]], *, missing: list[str] | None = None
) -> EvidenceReviewDraft:
    return EvidenceReviewDraft.model_validate(
        {"selections": selections, "missing": missing or []}
    )


def _padded_script(first: Any, *, slots: int) -> list[Any]:
    """Run単位化後は1回で完結する前提のscript。

    現行のtaskごと呼び出しでも枯渇crashにならないよう、余剰callへは
    空draftを充てる。
    """
    return [first] + [_EMPTY_DRAFT] * (slots - 1)


def _query_draft(queries: list[str]) -> ExternalQueryDraft:
    return ExternalQueryDraft(queries=queries)


def _task(goal: str, queries: list[str]) -> ResearchTask:
    return ResearchTask(research_goal=goal, article_search_queries=queries)


def _plan(
    *tasks: ResearchTask,
    target_time_window: TargetTimeWindow | None = _TARGET_TIME_WINDOW,
) -> SearchPlan:
    return SearchPlan(research_tasks=list(tasks), target_time_window=target_time_window)


def _internal_hit(
    *,
    assessment_id: int,
    curation_id: int,
    title: str,
    summary: str | None = None,
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id,
        title=title,
        summary=summary or f"{title} summary",
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


def _external_candidate(url: str, *, title: str) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(
        url=url,
        title=title,
        snippet="snippet",
        source_name="Example",
        published_at=AS_OF,
    )


class _Preparer:
    def __init__(
        self, *, content_requirements: list[AnswerRequirement] | None = None
    ) -> None:
        self._content_requirements = content_requirements or []

    async def prepare(self, **_kwargs: object) -> QuestionContextPreparationResult:
        return QuestionContextPreparationResult(
            context=QuestionContext(
                standalone_question="NVIDIA の見通しは？",
                content_requirements=self._content_requirements,
            ),
            telemetry=QuestionContextTelemetry(),
        )


class _Planner:
    def __init__(self, plan: SearchPlan) -> None:
        self._plan = plan

    async def plan(self, request: object) -> SearchPlan:
        del request
        return self._plan


class _UnreachableDirectAnswerer:
    async def answer(
        self, *, request: AnsweringRequest, previous_answer: str = ""
    ) -> DirectAnswerDraft:
        raise AssertionError(
            f"direct answer must not run: {request!r} {previous_answer!r}"
        )


class _EvidenceAnswerer:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[Any],
        target_time_window: TargetTimeWindow | None,
    ) -> EvidenceAnswerDraft:
        del request, target_time_window
        self.calls.append(list(evidence))
        if evidence:
            return EvidenceAnswerDraft(
                sufficiency="answered",
                answer="根拠に基づく回答です。",
                cited_refs=[item.source.source_ref for item in evidence],
            )
        return EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠が不足しています。",
            missing_aspects=["根拠が不足しています"],
        )


class _Scope(AbstractAsyncContextManager[ExternalResearchRuntime]):
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime

    async def __aenter__(self) -> ExternalResearchRuntime:
        return self._runtime

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        del exc_type, exc, tb
        return False


class _Factory:
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime

    def activate(self) -> _Scope:
        return _Scope(self._runtime)


class _InternalTool:
    """queryをkeyにhits/error/待ち合わせを切り替えるfake。task間を独立制御する。"""

    def __init__(
        self,
        *,
        hits_by_query: dict[str, list[InternalArticleSearchHit]] | None = None,
        errors_by_query: dict[str, BaseException] | None = None,
        gate_by_query: dict[str, asyncio.Event] | None = None,
        started_by_query: dict[str, asyncio.Event] | None = None,
    ) -> None:
        self._hits_by_query = hits_by_query or {}
        self._errors_by_query = errors_by_query or {}
        self._gate_by_query = gate_by_query or {}
        self._started_by_query = started_by_query or {}
        self.calls: list[Any] = []

    @property
    def name(self) -> str:
        return "internal_search"

    async def invoke(self, input: Any) -> list[InternalArticleSearchHit]:
        self.calls.append(input)
        query = input.queries.queries[0]
        started = self._started_by_query.get(query)
        if started is not None:
            started.set()
        gate = self._gate_by_query.get(query)
        if gate is not None:
            await gate.wait()
        if query in self._errors_by_query:
            raise self._errors_by_query[query]
        return list(self._hits_by_query.get(query, []))


class _ExternalTool:
    def __init__(
        self, results_by_query: dict[str, list[ExternalSearchCandidate]] | None = None
    ) -> None:
        self._results = results_by_query or {}
        self.calls: list[Any] = []

    @property
    def name(self) -> str:
        return "external_search"

    async def invoke(self, input: Any) -> list[ExternalSearchCandidate]:
        self.calls.append(input)
        return list(self._results.get(input.query, []))


class _Events:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


def _runner(
    *,
    plan: SearchPlan,
    query_runtime: object,
    reviewer_runtime: object,
    external_tool: object,
    internal_tool: object,
    events: object | None = None,
    content_requirements: list[AnswerRequirement] | None = None,
) -> tuple[AnsweringRunner, _EvidenceAnswerer]:
    answerer = _EvidenceAnswerer()
    runtime = ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        reviewer_runtime=reviewer_runtime,  # type: ignore[arg-type]
        search_tool=external_tool,  # type: ignore[arg-type]
    )
    phases = AnsweringPhases(
        planner=_Planner(plan),
        researcher=Researcher(internal_search=internal_tool, events=events),  # type: ignore[arg-type]
        reviewer=EvidenceReviewer(),
        external_runtime_factory=_Factory(runtime),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(content_requirements=content_requirements),
        phases_factory=lambda: phases,
        events=events,  # type: ignore[arg-type]
    )
    return runner, answerer


async def _run(runner: AnsweringRunner) -> Any:
    return await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
    )


# --- A. 精査の呼び出し単位 -------------------------------------------------


@pytest.mark.asyncio
async def test_review_runs_once_for_a_three_task_search_plan() -> None:
    """S1 A1。3 task の SearchPlan で reviewer の review が1回だけ呼ばれる。"""
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="hit-a")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="hit-b")
            ],
            "query-c": [
                _internal_hit(assessment_id=1003, curation_id=3, title="hit-c")
            ],
        }
    )
    reviewer_runtime = ScriptedAgentRuntime(
        _padded_script(
            _draft([{"candidate_index": 0, "claim": "c", "why_selected": "w"}]), slots=3
        )
    )
    runner, _answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    await _run(runner)

    assert len(reviewer_runtime.calls) == 1


@pytest.mark.asyncio
async def test_review_does_not_start_before_every_tasks_collection_completes() -> None:
    """S1 A2。最も遅いtaskの収集完了が精査開始の条件になる。

    現行は各taskが自分の収集完了ごとに個別reviewを呼ぶため、task-Aが
    止まっている間にtask-B/Cのreviewが先に走りred。
    """
    gate_a = asyncio.Event()
    started_a = asyncio.Event()
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="hit-a")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="hit-b")
            ],
            "query-c": [
                _internal_hit(assessment_id=1003, curation_id=3, title="hit-c")
            ],
        },
        gate_by_query={"query-a": gate_a},
        started_by_query={"query-a": started_a},
    )
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    reviewer_runtime = ScriptedAgentRuntime(
        _padded_script(
            _draft([{"candidate_index": 0, "claim": "c", "why_selected": "w"}]), slots=3
        )
    )
    runner, _answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    running = asyncio.create_task(_run(runner))
    try:
        await asyncio.wait_for(started_a.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        assert reviewer_runtime.calls == []
    finally:
        gate_a.set()
        await asyncio.wait_for(running, timeout=1.0)

    assert len(reviewer_runtime.calls) >= 1


@pytest.mark.asyncio
async def test_review_is_skipped_when_every_task_has_no_candidates() -> None:
    """S1 A3。全taskの候補が内外ともゼロのとき reviewer を呼ばない。"""
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    reviewer_runtime = ScriptedAgentRuntime([])
    runner, answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=_InternalTool(),
    )

    result = await _run(runner)

    assert reviewer_runtime.calls == []
    assert answerer.calls == [[]]
    assert result.final_output.status == "insufficient"


@pytest.mark.asyncio
async def test_review_still_runs_using_the_candidates_that_survive_a_failed_task() -> (
    None
):
    """S1 A4。いずれかのtaskの収集が失敗しても、残った候補で精査が走る。"""
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="A-hit")
            ],
            "query-c": [
                _internal_hit(assessment_id=1003, curation_id=3, title="C-hit")
            ],
        },
        errors_by_query={"query-b": InternalSearchError(phase="article_search")},
    )
    # task-Bの収集が失敗し候補ゼロになる分、統合index空間から外れる
    # (仮定: task昇順で結合。task-A→0、task-C→1)。
    reviewer_runtime = ScriptedAgentRuntime(
        _padded_script(
            _draft(
                [
                    {"candidate_index": 0, "claim": "A claim", "why_selected": "w"},
                    {"candidate_index": 1, "claim": "C claim", "why_selected": "w"},
                ]
            ),
            slots=2,
        )
    )
    runner, answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    await _run(runner)

    assert len(reviewer_runtime.calls) >= 1
    titles = {item.source.title for item in answerer.calls[0]}
    assert titles == {"A-hit", "C-hit"}


# --- B. 候補の渡し方 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_single_review_call_input_includes_every_tasks_research_goal() -> None:
    """S1 B1。reviewer入力に全taskのresearch_goalがグループとして含まれる。

    入力はAgent宣言のinput_renderer(production未変更の安定境界)を通して
    文字列化し、内部field名を前提にしない。
    """
    tasks = [
        _task("goal-alpha-unique", ["query-a"]),
        _task("goal-beta-unique", ["query-b"]),
        _task("goal-gamma-unique", ["query-c"]),
    ]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="hit-a")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="hit-b")
            ],
            "query-c": [
                _internal_hit(assessment_id=1003, curation_id=3, title="hit-c")
            ],
        }
    )
    reviewer_runtime = ScriptedAgentRuntime(_padded_script(_EMPTY_DRAFT, slots=3))
    runner, _answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    await _run(runner)

    rendered = EVIDENCE_REVIEWER_AGENT.prompt.input_renderer(
        reviewer_runtime.calls[0].input
    )
    assert all(
        goal in rendered
        for goal in ("goal-alpha-unique", "goal-beta-unique", "goal-gamma-unique")
    )


@pytest.mark.asyncio
async def test_content_requirements_appear_once_not_once_per_task_group() -> None:
    """S1 B4(仕様「候補の渡し方」)。

    content_requirementsはグループの外に1つだけ置かれ、goalごとに複写されない。
    """
    marker = "UNIQUE_REQUIREMENT_MARKER_7f2a"
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="hit-a")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="hit-b")
            ],
            "query-c": [
                _internal_hit(assessment_id=1003, curation_id=3, title="hit-c")
            ],
        }
    )
    reviewer_runtime = ScriptedAgentRuntime(_padded_script(_EMPTY_DRAFT, slots=3))
    runner, _answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
        content_requirements=[
            AnswerRequirement(requirement_id="c1", description=marker)
        ],
    )

    await _run(runner)

    # calls[0]だけでなく全callを見る: taskごとに別々の入力へ複写されると
    # 呼び出し回数分だけ出現してしまうため、Run全体でちょうど1回であることを
    # 検証する必要がある。
    combined = "\n".join(
        EVIDENCE_REVIEWER_AGENT.prompt.input_renderer(call.input)
        for call in reviewer_runtime.calls
    )
    assert combined.count(marker) == 1


@pytest.mark.asyncio
async def test_review_input_excludes_external_urls_across_every_task() -> None:
    """S1 B5(既存制約の維持)。候補projectionはURLを含まない(統合後の回帰guard)。"""
    tasks = [_task("goal-A", ["query-a"]), _task("goal-B", ["query-b"])]
    secret_url_a = "https://example.com/task-a-secret-7f21"
    secret_url_b = "https://example.com/task-b-secret-8c92"
    reviewer_runtime = ScriptedAgentRuntime(_padded_script(_EMPTY_DRAFT, slots=2))
    runner, _answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["qa"]), _query_draft(["qb"])]
        ),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(
            {
                "qa": [_external_candidate(secret_url_a, title="task-a headline")],
                "qb": [_external_candidate(secret_url_b, title="task-b headline")],
            }
        ),
        internal_tool=_InternalTool(),
    )

    await _run(runner)

    combined = "\n".join(
        EVIDENCE_REVIEWER_AGENT.prompt.input_renderer(call.input)
        for call in reviewer_runtime.calls
    )
    assert secret_url_a not in combined
    assert secret_url_b not in combined


# --- C. 選別結果の復元 -------------------------------------------------------


@pytest.mark.asyncio
async def test_selection_restores_the_right_candidate_and_task_across_groups() -> None:
    """S1 C1(B2/B3を包含)。グループをまたいだindexから候補と所属taskが復元される。

    「research_goalごとにグループ化しtask_index昇順で結合、グループ内は
    内部候補が先・外部候補が後」という仕様の唯一自然な解釈から、fixtureの
    候補件数だけを根拠にRun全体の通し番号を導出する
    (仕様「候補の渡し方」「選別結果の復元」)。
    """
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="A-int-1"),
                _internal_hit(assessment_id=1002, curation_id=2, title="A-int-2"),
            ],
            "query-b": [
                _internal_hit(assessment_id=1003, curation_id=3, title="B-int-1")
            ],
            "query-c": [],
        }
    )
    # 統合index空間(仮定): task-A(0,1) task-B(2内部/3,4外部) task-C(5外部)。
    reviewer_runtime = ScriptedAgentRuntime(
        _padded_script(
            _draft(
                [
                    {
                        "candidate_index": 1,
                        "claim": "A-int-2 claim",
                        "why_selected": "w",
                    },
                    {
                        "candidate_index": 3,
                        "claim": "B-ext-1 claim",
                        "why_selected": "w",
                    },
                    {
                        "candidate_index": 5,
                        "claim": "C-ext-1 claim",
                        "why_selected": "w",
                    },
                ]
            ),
            slots=3,
        )
    )
    runner, answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime(
            [_query_draft([]), _query_draft(["qb"]), _query_draft(["qc"])]
        ),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(
            {
                "qb": [
                    _external_candidate("https://example.com/b-ext-1", title="B-ext-1"),
                    _external_candidate("https://example.com/b-ext-2", title="B-ext-2"),
                ],
                "qc": [
                    _external_candidate("https://example.com/c-ext-1", title="C-ext-1")
                ],
            }
        ),
        internal_tool=internal_tool,
    )

    await _run(runner)

    titles = {item.source.title for item in answerer.calls[0]}
    assert titles == {"A-int-2", "B-ext-1", "C-ext-1"}


@pytest.mark.asyncio
async def test_out_of_range_duplicate_and_over_cap_selections_drop_at_run_level() -> (
    None
):
    """S1 C2。範囲外index・重複index・Run採用上限が決定的にdropされる。

    S2でcap値がRun単位の15になったため、16件目以降の超過で検証する。
    """
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(
                    assessment_id=1000 + i, curation_id=100 + i, title=f"cand-{i}"
                )
                for i in range(6)
            ],
            "query-b": [
                _internal_hit(
                    assessment_id=1000 + i, curation_id=100 + i, title=f"cand-{i}"
                )
                for i in range(6, 12)
            ],
            "query-c": [
                _internal_hit(
                    assessment_id=1000 + i, curation_id=100 + i, title=f"cand-{i}"
                )
                for i in range(12, 17)
            ],
        }
    )
    # 統合index空間(仮定): task-A(0-5) task-B(6-11) task-C(12-16)、合計17候補。
    # 0を重複、99を範囲外、有効17件のうち16件目以降(index 15,16)は
    # 上限15件超過でdrop。
    selections = [
        {"candidate_index": index, "claim": f"claim-{index}", "why_selected": "w"}
        for index in [0, 0, *range(1, 17), 99]
    ]
    reviewer_runtime = ScriptedAgentRuntime(_padded_script(_draft(selections), slots=3))
    runner, answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    await _run(runner)

    assert len(answerer.calls[0]) == 15


@pytest.mark.asyncio
async def test_same_url_selected_from_two_tasks_are_both_kept() -> None:
    """S1 C4。同じURLの外部候補が複数taskで採用されたとき、両方が根拠として残る。

    `deduplicate_external_evidence_by_url()`は仕様上廃止対象(「合流と重複排除」)
    だが、本sliceではまだ呼ばれ続けるためred。
    """
    shared_url = "https://example.com/shared-story"
    tasks = [_task("goal-A", ["query-a"]), _task("goal-B", ["query-b"])]
    reviewer_runtime = ScriptedAgentRuntime(
        _padded_script(
            _draft(
                [
                    {"candidate_index": 0, "claim": "task0 view", "why_selected": "w"},
                    {"candidate_index": 1, "claim": "task1 view", "why_selected": "w"},
                ]
            ),
            slots=2,
        )
    )
    runner, answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["qa"]), _query_draft(["qb"])]
        ),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(
            {
                "qa": [_external_candidate(shared_url, title="task0 headline")],
                "qb": [_external_candidate(shared_url, title="task1 headline")],
            }
        ),
        internal_tool=_InternalTool(),
    )

    await _run(runner)

    kept_titles = [item.source.title for item in answerer.calls[0]]
    assert kept_titles == ["task0 headline", "task1 headline"]
    assert sum(shared_url in str(item.source.url) for item in answerer.calls[0]) == 2


# --- D. 不足の表明 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_flows_as_a_single_run_level_list_not_merged_per_task() -> None:
    """S1 D1/D3。reviewerのmissingはRun単位で1本としてmissing_aspectsへ流れ、

    非空ならstatusはinsufficientになる。現行はtaskごとにmissingを集めて
    連結するため(result_assembly._external_task_missing())、他taskへ充てた
    poison entryの文言まで混入しred。
    """
    tasks = [_task("goal-A", ["query-a"]), _task("goal-B", ["query-b"])]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="hit-a")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="hit-b")
            ],
        }
    )
    intended_draft = _draft(
        [
            {"candidate_index": 0, "claim": "claim-a", "why_selected": "w"},
            {"candidate_index": 1, "claim": "claim-b", "why_selected": "w"},
        ],
        missing=["run全体の不足X"],
    )
    poison_draft = _draft([], missing=["taskごとに混入してはいけない不足Y"])
    reviewer_runtime = ScriptedAgentRuntime([intended_draft, poison_draft])
    runner, answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    result = await _run(runner)

    assert result.final_output.missing_aspects == ["run全体の不足X"]
    assert result.final_output.status == "insufficient"
    assert len(answerer.calls[0]) == 2


@pytest.mark.asyncio
async def test_incomplete_task_adds_the_fixed_phrase_exactly_once() -> None:
    """S1 D2。収集が完了しなかったtaskがある場合、固定文言が1行だけ加わる。

    発火条件は現行のper-task review in (failed, skipped_empty) と等価に
    なるよう、収集失敗task(内部エラー)から導出される。
    """
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="A-hit")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="B-hit")
            ],
        },
        errors_by_query={"query-c": InternalSearchError(phase="article_search")},
    )
    # task-Cは収集失敗で候補ゼロのため統合index空間から外れる(task-A→0、task-B→1)。
    reviewer_runtime = ScriptedAgentRuntime(
        _padded_script(
            _draft(
                [
                    {"candidate_index": 0, "claim": "A claim", "why_selected": "w"},
                    {"candidate_index": 1, "claim": "B claim", "why_selected": "w"},
                ]
            ),
            slots=3,
        )
    )
    runner, _answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    result = await _run(runner)

    assert (
        result.final_output.missing_aspects.count("完了できなかった調査があります") == 1
    )


# --- E. 精査の失敗 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_failure_after_two_attempts_empties_the_whole_run() -> None:
    """S1 E1/E2/E3。reviewerが2 attempt失敗したRunは根拠ゼロで終わり、

    固定文言missingでstatus=insufficientになり、進捗eventは発火しない。

    scriptを[失敗, 失敗, 成功draft]にする(REVISE 1)。Run単位1回なら2 attempt
    を使い切った時点で打ち切られ成功draftへは到達しない。per-task実装なら
    task0が2 attemptで失敗した後、task1が新しいattempt列として成功draftを
    消費し根拠を返してしまうため、Run全体が空であるべきというassertが落ちる。
    [failure] * N のように常に失敗するscriptでは呼び出し回数によらず結果が
    一致してしまい、構造の差を判別できない(非空虚化)。
    """
    # 2 taskに限定する(REVISE 1): 3件以上だと3個目のtaskが
    # per-task実装でも自分のattempt列を持てずscript枯渇crashになり、
    # 「構造の差で落ちる」という意図から外れてしまう。
    tasks = [_task("goal-A", ["query-a"]), _task("goal-B", ["query-b"])]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="hit-a")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="hit-b")
            ],
        }
    )
    failure = AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    success_draft = _draft(
        [{"candidate_index": 0, "claim": "claim", "why_selected": "w"}]
    )
    reviewer_runtime = ScriptedAgentRuntime([failure, failure, success_draft])
    events = _Events()
    runner, answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
        events=events,
    )

    result = await _run(runner)

    selected_events = [
        event
        for event in events.events
        if event.type == "external_search.evidence_selected"
    ]
    assert answerer.calls == [[]]
    assert result.final_output.sources == []
    assert result.final_output.status == "insufficient"
    assert (
        "回答に使える根拠を取得できませんでした" in result.final_output.missing_aspects
    )
    assert selected_events == []


# --- F. 進捗event ------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_events_fire_ascending_by_task_index_after_review_succeeds() -> (
    None
):
    """S1 F1/F2/F3。精査成功後、候補があったtaskについてtask_index昇順で

    1回ずつ発火し、evidence_countは当該task由来の採用件数と一致する。
    候補ゼロだったtaskには発火しない。task-Aを遅延させ、完了順(B→A)と
    task_index順(A→B)を意図的にずらしてorderingの不変条件を検証する。
    """
    gate_a = asyncio.Event()
    started_a = asyncio.Event()
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="A-hit")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="B-hit")
            ],
            "query-c": [],
        },
        gate_by_query={"query-a": gate_a},
        started_by_query={"query-a": started_a},
    )
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    events = _Events()
    # 統合index空間(仮定): task-A(0) task-B(1)。task-Cは候補ゼロで現れない。
    reviewer_runtime = ScriptedAgentRuntime(
        _padded_script(
            _draft(
                [
                    {"candidate_index": 0, "claim": "A claim", "why_selected": "w"},
                    {"candidate_index": 1, "claim": "B claim", "why_selected": "w"},
                ]
            ),
            slots=2,
        )
    )
    runner, _answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
        events=events,
    )

    running = asyncio.create_task(_run(runner))
    await asyncio.wait_for(started_a.wait(), timeout=1.0)
    # task-B/Cを先に完了させてから、task-Aを最後に解放する。
    await asyncio.sleep(0.05)
    gate_a.set()
    await asyncio.wait_for(running, timeout=1.0)

    selected = [
        event
        for event in events.events
        if event.type == "external_search.evidence_selected"
    ]
    assert [(event.task_index, event.evidence_count) for event in selected] == [
        (0, 1),
        (1, 1),
    ]


# --- G. 非露出 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_spans_and_events_do_not_expose_untrusted_text(
    capfire: CaptureLogfire,
) -> None:
    """S1 G1(既存制約の維持)。span属性・eventに候補snippet・研究goalが載らない。

    Run単位化で1回のreviewer phase spanへ複数taskの内容が集約されても、
    非露出制約が保たれることを確認する回帰テスト。
    """
    secret_snippet = "SECRET_SNIPPET_MARKER_91ac"
    secret_goal = "SECRET_GOAL_TEXT_44bd"
    tasks = [_task(secret_goal, ["query-a"]), _task("goal-B", ["query-b"])]
    internal_tool = _InternalTool(
        hits_by_query={
            "query-a": [
                _internal_hit(
                    assessment_id=1001,
                    curation_id=1,
                    title="hit-a",
                    summary=secret_snippet,
                )
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="hit-b")
            ],
        }
    )
    reviewer_runtime = ScriptedAgentRuntime(_padded_script(_EMPTY_DRAFT, slots=2))
    events = _Events()
    runner, _answerer = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_tool=_ExternalTool(),
        internal_tool=internal_tool,
        events=events,
    )

    await _run(runner)

    span_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(), ensure_ascii=False, default=str
    )
    event_dump = json.dumps(
        [event.model_dump(mode="json") for event in events.events], ensure_ascii=False
    )
    assert secret_snippet not in span_dump
    assert secret_goal not in span_dump
    assert secret_snippet not in event_dump
    assert secret_goal not in event_dump
