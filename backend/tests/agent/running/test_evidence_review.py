"""AnsweringRunner: Evidence Reviewer をRun単位1回へ広げる契約(S1)。

backend/specs/evidence-review-run-scope-slice.md の Invariants / Test contract を
`AnsweringRunner.run()` の黒箱から検証する。

新しいグループ化option入力・Run全体の通し番号indexは、spec上の field名が
固定されていないため、この正本テストは以下の安定境界だけを覗く:
- reviewer呼び出し回数と入力(`ScriptedAgentRuntime.calls`)
- 入力の可視文字列化(`EVIDENCE_REVIEWER_AGENT.prompt.input_renderer`。
  production未変更のAgent宣言契約が持つ安定した拡張点)
- reviewerの出力型(`EvidenceReviewerDraft`。仕様上変更されない応答契約)
- `AnsweringRunner.run()` の最終出力・event(仕様上変更されないSSE/response契約)

複数taskにまたがるoption_indexを使うテストでは、
「research_goalごとにグループ化し、グループ内は内部の選択肢が先・外部が後、
グループはtask_index昇順で結合する」という仕様の唯一自然な解釈から
Run全体の通し番号を入力fixtureの選択肢件数から導出する。
reviewer の script は「Run 単位で 1 回」ちょうどの 1 件だけ渡す。2 回目の
呼び出し (task 単位への退行) は script 枯渇の AssertionError で即 red になる。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.contract import EvidenceAnswerOutcome
from app.agent.evidence_collection import NewsCollector, Researcher
from app.agent.evidence_collection.external_search import ExternalSearchService
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
    InternalSearchError,
)
from app.agent.evidence_review import (
    EvidenceReviewer,
    EvidenceReviewerDraft,
    EvidenceRunFailed,
)
from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.planning.contract import ResearchTask, SearchPlan, TargetTimeWindow
from app.agent.running import AnsweringPhases, AnsweringRunner
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from tests.agent.running._harness import (
    DEFAULT_TARGET_TIME_WINDOW as _TARGET_TIME_WINDOW,
)
from tests.agent.running._harness import (
    Events as _Events,
)
from tests.agent.running._harness import (
    EvidenceAnswerer as _EvidenceAnswerer,
)
from tests.agent.running._harness import (
    FakeExternalSearchGateway as _FakeExternalSearchGateway,
)
from tests.agent.running._harness import (
    Planner as _Planner,
)
from tests.agent.running._harness import (
    Preparer as _Preparer,
)
from tests.agent.running._harness import (
    UnreachableDirectAnswerer as _UnreachableDirectAnswerer,
)
from tests.agent.running._harness import (
    capture_external_outcome as _capture_external_outcome,
)
from tests.agent.running._harness import (
    execute_run as _run,
)
from tests.agent.running._harness import (
    external_hit as _external_hit,
)
from tests.agent.running._harness import (
    internal_hit as _internal_hit,
)
from tests.agent.running._harness import (
    query_draft as _query_draft,
)
from tests.agent.running._harness import (
    review_draft as _draft,
)
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._span_helpers import one_span_named

_EMPTY_DRAFT = EvidenceReviewerDraft.model_validate({"selections": [], "missing": []})


def _task(goal: str, queries: list[str]) -> ResearchTask:
    return ResearchTask(research_goal=goal, article_search_queries=queries)


def _plan(
    *tasks: ResearchTask,
    target_time_window: TargetTimeWindow | None = _TARGET_TIME_WINDOW,
) -> SearchPlan:
    return SearchPlan(research_tasks=list(tasks), target_time_window=target_time_window)


class _Scope(AbstractAsyncContextManager[ExternalResearchRuntime]):
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime
        self.entered = False
        self.exit_calls = 0

    async def __aenter__(self) -> ExternalResearchRuntime:
        self.entered = True
        return self._runtime

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        del exc_type, exc, tb
        self.exit_calls += 1
        return False


class _Factory:
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime
        self.scopes: list[_Scope] = []

    def activate(self) -> _Scope:
        scope = _Scope(self._runtime)
        self.scopes.append(scope)
        return scope


class _FakeInternalSearch:
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

    async def search(self, queries: Any) -> list[InternalArticleSearchHit]:
        self.calls.append(queries)
        query = queries.queries[0]
        started = self._started_by_query.get(query)
        if started is not None:
            started.set()
        gate = self._gate_by_query.get(query)
        if gate is not None:
            await gate.wait()
        if query in self._errors_by_query:
            raise self._errors_by_query[query]
        return list(self._hits_by_query.get(query, []))


def _runner(
    *,
    plan: SearchPlan,
    query_runtime: object,
    reviewer_runtime: object,
    external_gateway: object,
    internal_search: object,
    events: object | None = None,
    answer_requirements: tuple[str, ...] | None = None,
    answerer: _EvidenceAnswerer | None = None,
) -> tuple[AnsweringRunner, _EvidenceAnswerer, _Factory]:
    answerer = answerer or _EvidenceAnswerer()
    runtime = ExternalResearchRuntime(
        external_search=ExternalSearchService(
            query_runtime=query_runtime,  # type: ignore[arg-type]
            search_gateway=external_gateway,  # type: ignore[arg-type]
        ),
        reviewer_runtime=reviewer_runtime,  # type: ignore[arg-type]
    )
    factory = _Factory(runtime)
    phases = AnsweringPhases(
        planner=_Planner(plan),
        collector=NewsCollector(
            researcher=Researcher(internal_search=internal_search, events=events),  # type: ignore[arg-type]
        ),
        reviewer=EvidenceReviewer(),
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(answer_requirements=answer_requirements),
        phases_factory=lambda: phases,
        events=events,  # type: ignore[arg-type]
    )
    return runner, answerer, factory


class _ReviewMissingCapturingAnswerer(_EvidenceAnswerer):
    def __init__(self) -> None:
        super().__init__()
        self.review_missing_calls: list[tuple[str, ...]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[Any],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerOutcome:
        self.review_missing_calls.append(review_missing)
        return await super().answer(
            request=request,
            evidence=evidence,
            target_time_window=target_time_window,
            review_missing=review_missing,
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
    internal_search = _FakeInternalSearch(
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
        [_draft([{"option_index": 0, "claim": "c", "why_selected": "w"}])]
    )
    runner, _answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
    )

    await _run(runner)

    assert len(reviewer_runtime.calls) == 1
    reviewer_runtime.assert_all_outcomes_consumed()


@pytest.mark.asyncio
async def test_review_does_not_start_before_every_tasks_collection_completes() -> None:
    """S1 A2。最も遅いtaskの収集完了が精査開始の条件になる。"""
    gate_a = asyncio.Event()
    started_a = asyncio.Event()
    internal_search = _FakeInternalSearch(
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
        [_draft([{"option_index": 0, "claim": "c", "why_selected": "w"}])]
    )
    runner, _answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
    )

    running = asyncio.create_task(_run(runner))
    try:
        await asyncio.wait_for(started_a.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        assert reviewer_runtime.calls == []
    finally:
        gate_a.set()
        await asyncio.wait_for(running, timeout=1.0)

    assert len(reviewer_runtime.calls) == 1


@pytest.mark.asyncio
async def test_review_is_skipped_when_every_task_has_no_hits() -> None:
    """S1 A3。全taskのヒットが内外ともゼロのとき reviewer を呼ばず、

    選別eventも発火しない。
    """
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    reviewer_runtime = ScriptedAgentRuntime([])
    events = _Events()
    runner, answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=_FakeInternalSearch(),
        events=events,
    )

    result = await _run(runner)

    selected = [
        event for event in events.events if event.type == "evidence_review.selected"
    ]
    assert reviewer_runtime.calls == []
    assert answerer.calls == [[]]
    assert result.final_output.status == "insufficient"
    assert selected == []


@pytest.mark.asyncio
async def test_review_still_runs_using_the_hits_that_survive_a_failed_task() -> None:
    """S1 A4。いずれかのtaskの収集が失敗しても、残ったヒットで精査が走る。"""
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_search = _FakeInternalSearch(
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
    # task-Bの収集が失敗しヒットゼロになる分、統合index空間から外れる
    # (仮定: task昇順で結合。task-A→0、task-C→1)。
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _draft(
                [
                    {"option_index": 0, "claim": "A claim", "why_selected": "w"},
                    {"option_index": 1, "claim": "C claim", "why_selected": "w"},
                ]
            )
        ]
    )
    runner, answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
    )

    await _run(runner)

    assert len(reviewer_runtime.calls) == 1
    titles = {item.source.title for item in answerer.calls[0]}
    assert titles == {"A-hit", "C-hit"}


@pytest.mark.asyncio
async def test_task_with_only_internal_hits_still_reaches_review() -> None:
    """保証するテスト条件 7(内部のみ)。外部全滅でも精査へ進み内部根拠を返す。"""
    internal_search = _FakeInternalSearch(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="internal only")
            ]
        }
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [_draft([{"option_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    runner, answerer, _factory = _runner(
        plan=_plan(_task("internal only task", ["query-a"])),
        query_runtime=ScriptedAgentRuntime([_query_draft([])]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
    )

    result = await _run(runner)

    assert len(reviewer_runtime.calls) == 1
    assert [item.source.title for item in answerer.calls[0]] == ["internal only"]
    assert result.final_output.status == "answered"


@pytest.mark.asyncio
async def test_task_with_only_external_hits_still_reaches_review() -> None:
    """保証するテスト条件 7(外部のみ)。内部失敗でも精査へ進み外部根拠を返す。"""
    reviewer_runtime = ScriptedAgentRuntime(
        [_draft([{"option_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    runner, answerer, _factory = _runner(
        plan=_plan(_task("external only task", ["query-a"])),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(
            {"q": [_external_hit("https://example.com/only")]}
        ),
        internal_search=_FakeInternalSearch(
            errors_by_query={"query-a": InternalSearchError(phase="article_search")}
        ),
    )

    result = await _run(runner)

    assert len(reviewer_runtime.calls) == 1
    assert [item.source.title for item in answerer.calls[0]] == ["only"]
    # D4-S2: run単位のcollection_failuresは廃止された。このtaskはreview=
    # succeeded(外部ヒットを精査して採用)で完了扱いになるため、内部収集が
    # 失敗していてもstatusはansweredになる。
    assert result.final_output.status == "answered"


@pytest.mark.asyncio
async def test_time_filter_failure_still_activates_external_scope_for_review() -> None:
    """保証するテスト条件 11。time filter失敗でもreviewerのLLM runtimeが使えるよう
    external runtime scopeがactivateされる(direct pathの非activateは不変)。
    """
    internal_search = _FakeInternalSearch(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="internal hit")
            ]
        }
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [_draft([{"option_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    runner, answerer, factory = _runner(
        plan=_plan(
            _task("closed by time filter", ["query-a"]),
            target_time_window=TargetTimeWindow(kind="unsupported_explicit_window"),
        ),
        query_runtime=ScriptedAgentRuntime([]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
    )

    await _run(runner)

    assert len(factory.scopes) == 1
    assert factory.scopes[0].entered is True
    assert factory.scopes[0].exit_calls == 1
    assert len(reviewer_runtime.calls) == 1
    assert [item.source.title for item in answerer.calls[0]] == ["internal hit"]


# --- B. 選択肢の渡し方 ---------------------------------------------------------


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
    internal_search = _FakeInternalSearch(
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
    reviewer_runtime = ScriptedAgentRuntime([_EMPTY_DRAFT])
    runner, _answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
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
async def test_review_input_never_carries_answer_requirements() -> None:
    """v3(Evidence Review「Evidence Review(v2 -> v3)」)。

    question_contextのanswer_requirementsはevidence_reviewへの配線が撤去され、
    reviewerはresearch_goalだけで判定する。AnswerBrief側に要件があっても
    reviewer入力・render結果には一切現れない。
    """
    marker = "UNIQUE_REQUIREMENT_MARKER_7f2a"
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_search = _FakeInternalSearch(
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
    reviewer_runtime = ScriptedAgentRuntime([_EMPTY_DRAFT])
    runner, _answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
        answer_requirements=(marker,),
    )

    await _run(runner)

    assert not hasattr(reviewer_runtime.calls[0].input, "answer_requirements")
    combined = "\n".join(
        EVIDENCE_REVIEWER_AGENT.prompt.input_renderer(call.input)
        for call in reviewer_runtime.calls
    )
    assert marker not in combined


@pytest.mark.asyncio
async def test_review_input_excludes_external_urls_across_every_task() -> None:
    """外部ヒットのURLは1回のRunを通じてreviewer入力に到達しない。

    LLMはindexで記事を指定して、出典URLはproductionがindexから復元する。
    投影型にurl fieldが無い構造保証はevidence_review/test_agent_declaration.pyが持つ。
    """
    tasks = [_task("goal-A", ["query-a"]), _task("goal-B", ["query-b"])]
    secret_url_a = "https://example.com/task-a-secret-7f21"
    secret_url_b = "https://example.com/task-b-secret-8c92"
    reviewer_runtime = ScriptedAgentRuntime([_EMPTY_DRAFT])
    runner, _answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["qa"]), _query_draft(["qb"])]
        ),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(
            {
                "qa": [_external_hit(secret_url_a, title="task-a headline")],
                "qb": [_external_hit(secret_url_b, title="task-b headline")],
            }
        ),
        internal_search=_FakeInternalSearch(),
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
async def test_selection_restores_the_right_hit_and_task_across_groups() -> None:
    """S1 C1(B2/B3を包含)。グループをまたいだindexから出所と所属taskが復元される。

    「research_goalごとにグループ化しtask_index昇順で結合、グループ内は
    内部ヒットが先・外部ヒットが後」という仕様の唯一自然な解釈から、fixtureの
    選択肢件数だけを根拠にRun全体の通し番号を導出する
    (仕様「選択肢の渡し方」「選別結果の復元」)。
    """
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    internal_search = _FakeInternalSearch(
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
        [
            _draft(
                [
                    {
                        "option_index": 1,
                        "claim": "A-int-2 claim",
                        "why_selected": "w",
                    },
                    {
                        "option_index": 3,
                        "claim": "B-ext-1 claim",
                        "why_selected": "w",
                    },
                    {
                        "option_index": 5,
                        "claim": "C-ext-1 claim",
                        "why_selected": "w",
                    },
                ]
            )
        ]
    )
    runner, answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime(
            [_query_draft([]), _query_draft(["qb"]), _query_draft(["qc"])]
        ),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(
            {
                "qb": [
                    _external_hit("https://example.com/b-ext-1", title="B-ext-1"),
                    _external_hit("https://example.com/b-ext-2", title="B-ext-2"),
                ],
                "qc": [_external_hit("https://example.com/c-ext-1", title="C-ext-1")],
            }
        ),
        internal_search=internal_search,
    )

    await _run(runner)

    titles = {item.source.title for item in answerer.calls[0]}
    assert titles == {"A-int-2", "B-ext-1", "C-ext-1"}


@pytest.mark.asyncio
async def test_same_url_selected_from_two_tasks_keeps_each_task_evidence() -> None:
    """同じURLでもtaskが異なる場合は両方のEvidenceをAnswererへ渡す。"""
    shared_url = "https://example.com/shared-story"
    tasks = [_task("goal-A", ["query-a"]), _task("goal-B", ["query-b"])]
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _draft(
                [
                    {"option_index": 0, "claim": "task0 view", "why_selected": "w"},
                    {"option_index": 1, "claim": "task1 view", "why_selected": "w"},
                ]
            )
        ]
    )
    runner, answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["qa"]), _query_draft(["qb"])]
        ),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(
            {
                "qa": [_external_hit(shared_url, title="task0 headline")],
                "qb": [_external_hit(shared_url, title="task1 headline")],
            }
        ),
        internal_search=_FakeInternalSearch(),
    )

    await _run(runner)

    kept_titles = [item.source.title for item in answerer.calls[0]]
    assert kept_titles == ["task0 headline", "task1 headline"]
    assert sum(shared_url in str(item.source.url) for item in answerer.calls[0]) == 2


@pytest.mark.asyncio
async def test_same_internal_article_from_two_tasks_keeps_each() -> None:
    """同じ内部検索の記事でもtaskが異なる場合は両方のEvidenceをAnswererへ渡す。"""
    shared_curation_id = 42
    internal_search = _FakeInternalSearch(
        hits_by_query={
            "query-a": [
                _internal_hit(
                    assessment_id=1001,
                    curation_id=shared_curation_id,
                    title="shared article (task0)",
                )
            ],
            "query-b": [
                _internal_hit(
                    assessment_id=1002,
                    curation_id=shared_curation_id,
                    title="shared article (task1)",
                )
            ],
        }
    )
    # 統合index空間: task昇順で結合し、task0の唯一の選択肢が0、task1が1。
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _draft(
                [
                    {"option_index": 0, "claim": "first claim", "why_selected": "w"},
                    {
                        "option_index": 1,
                        "claim": "second claim",
                        "why_selected": "w",
                    },
                ]
            )
        ]
    )
    runner, answerer, _factory = _runner(
        plan=_plan(_task("first task", ["query-a"]), _task("second task", ["query-b"])),
        query_runtime=ScriptedAgentRuntime([_query_draft([]), _query_draft([])]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
    )

    await _run(runner)

    titles = [item.source.title for item in answerer.calls[0]]
    assert titles == ["shared article (task0)", "shared article (task1)"]


# --- D. 不足の表明 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_flows_as_a_single_run_level_list_not_merged_per_task() -> None:
    """S1 D1/D3。reviewerのmissingはRun単位で1本としてmissing_aspectsへ流れ、

    非空ならstatusはinsufficientになる。他taskへ充てたpoison entryの
    文言は混入しない。
    """
    tasks = [_task("goal-A", ["query-a"]), _task("goal-B", ["query-b"])]
    internal_search = _FakeInternalSearch(
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
            {"option_index": 0, "claim": "claim-a", "why_selected": "w"},
            {"option_index": 1, "claim": "claim-b", "why_selected": "w"},
        ],
        missing=["run全体の不足X"],
    )
    poison_draft = _draft([], missing=["taskごとに混入してはいけない不足Y"])
    reviewer_runtime = ScriptedAgentRuntime([intended_draft, poison_draft])
    runner, answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
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
    internal_search = _FakeInternalSearch(
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
    # task-Cは収集失敗でヒットゼロのため統合index空間から外れる(task-A→0、task-B→1)。
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _draft(
                [
                    {"option_index": 0, "claim": "A claim", "why_selected": "w"},
                    {"option_index": 1, "claim": "B claim", "why_selected": "w"},
                ]
            )
        ]
    )
    runner, _answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
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
    internal_search = _FakeInternalSearch(
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
    success_draft = _draft([{"option_index": 0, "claim": "claim", "why_selected": "w"}])
    reviewer_runtime = ScriptedAgentRuntime([failure, failure, success_draft])
    events = _Events()
    runner, answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
        events=events,
    )

    result = await _run(runner)

    selected_events = [
        event for event in events.events if event.type == "evidence_review.selected"
    ]
    assert answerer.calls == [[]]
    assert result.final_output.sources == []
    assert result.final_output.status == "insufficient"
    assert (
        "回答に使える根拠を取得できませんでした" in result.final_output.missing_aspects
    )
    assert selected_events == []


@pytest.mark.asyncio
async def test_reviewer_failure_after_two_attempts_becomes_failed_evidence_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runnerがreviewer失敗をEvidenceRunFailedへ写す結線を保証する。

    attempt/timeout/失敗分類の詳細な組み合わせは
    tests/agent/evidence_review/test_reviewer.py が正本。
    """
    captured = _capture_external_outcome(monkeypatch)
    failure = AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)
    reviewer_runtime = ScriptedAgentRuntime([failure, failure])
    runner, answerer, _factory = _runner(
        plan=_plan(_task("reviewer failure", ["query-a"])),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(
            {"q": [_external_hit("https://example.com/q")]}
        ),
        internal_search=_FakeInternalSearch(),
    )

    await _run(runner)

    report = captured[0].collected_news.tasks[0].report
    evidence_run = captured[0].evidence_run
    assert (
        report.external_collection,
        isinstance(evidence_run, EvidenceRunFailed),
        evidence_run.failure_reason,
        answerer.calls,
    ) == ("succeeded", True, "response_not_json", [[]])


@pytest.mark.asyncio
async def test_failed_evidence_run_keeps_failure_reason_out_of_answerer_and_in_span(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    answerer = _ReviewMissingCapturingAnswerer()
    failure = AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)
    runner, _answerer, _factory = _runner(
        plan=_plan(_task("reviewer failure", ["query-a"])),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=ScriptedAgentRuntime([failure, failure]),
        external_gateway=_FakeExternalSearchGateway(
            {"q": [_external_hit("https://example.com/q")]}
        ),
        internal_search=_FakeInternalSearch(),
        answerer=answerer,
    )

    await _run(runner)

    evidence_run = captured[0].evidence_run
    span = one_span_named(capfire, "agent_answering_run")
    assert isinstance(evidence_run, EvidenceRunFailed)
    assert (
        answerer.review_missing_calls,
        span["attributes"]["review_failure_reason"],
        evidence_run.failure_reason,
    ) == ([()], "response_not_json", "response_not_json")


# --- F. 進捗event ------------------------------------------------------------


@pytest.mark.asyncio
async def test_selected_event_fires_once_for_the_whole_run_without_task_index() -> None:
    """S2。精査成功後、選別eventはRun全体で1本だけ発火する。

    3taskのうち2task(A/B)に採用対象があり、1task(C)はヒットゼロという構成でも
    本数は1本のまま増えない。evidence_countはtask横断の採用件数の合算になり、
    payloadはtask_indexを持たない。task単位で数え直す旧実装は
    「2本発火する」「task_indexが残る」のいずれかで必ず落ちる。
    """
    internal_search = _FakeInternalSearch(
        hits_by_query={
            "query-a": [
                _internal_hit(assessment_id=1001, curation_id=1, title="A-hit")
            ],
            "query-b": [
                _internal_hit(assessment_id=1002, curation_id=2, title="B-hit")
            ],
            "query-c": [],
        }
    )
    tasks = [
        _task("goal-A", ["query-a"]),
        _task("goal-B", ["query-b"]),
        _task("goal-C", ["query-c"]),
    ]
    events = _Events()
    # 統合index空間(仮定): task-A(0) task-B(1)。task-Cはヒットゼロで現れない。
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _draft(
                [
                    {"option_index": 0, "claim": "A claim", "why_selected": "w"},
                    {"option_index": 1, "claim": "B claim", "why_selected": "w"},
                ]
            )
        ]
    )
    runner, _answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
        events=events,
    )

    await _run(runner)

    selected = [
        event for event in events.events if event.type == "evidence_review.selected"
    ]
    assert len(selected) == 1
    assert selected[0].evidence_count == 2
    assert "task_index" not in selected[0].model_dump()


@pytest.mark.asyncio
async def test_evidence_selected_event_count_is_internal_plus_external() -> None:
    """selected.evidence_countは内部採用数と外部採用数の合算になる。"""
    events = _Events()
    internal_search = _FakeInternalSearch(
        hits_by_query={
            "query-a": [
                _internal_hit(
                    assessment_id=2001, curation_id=1001, title="internal hit"
                )
            ]
        }
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _draft(
                [
                    {
                        "option_index": 0,
                        "claim": "internal claim",
                        "why_selected": "why",
                    },
                    {
                        "option_index": 1,
                        "claim": "external claim",
                        "why_selected": "why",
                    },
                ]
            )
        ]
    )
    runner, _answerer, _factory = _runner(
        plan=_plan(_task("combined evidence", ["query-a"])),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q1"])]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(
            {"q1": [_external_hit("https://example.com/q1")]}
        ),
        internal_search=internal_search,
        events=events,
    )

    await _run(runner)

    selected_events = [
        event.model_dump()
        for event in events.events
        if event.type == "evidence_review.selected"
    ]
    assert selected_events == [
        {
            "type": "evidence_review.selected",
            "evidence_count": 2,
        }
    ]


# --- G. 非露出 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_spans_and_events_do_not_expose_untrusted_text(
    capfire: CaptureLogfire,
) -> None:
    """S1 G1(既存制約の維持)。span属性・eventに選択肢のsnippet・研究goalが載らない。

    Run単位化で1回のreviewer phase spanへ複数taskの内容が集約されても、
    非露出制約が保たれることを確認する回帰テスト。
    """
    secret_snippet = "SECRET_SNIPPET_MARKER_91ac"
    secret_goal = "SECRET_GOAL_TEXT_44bd"
    tasks = [_task(secret_goal, ["query-a"]), _task("goal-B", ["query-b"])]
    internal_search = _FakeInternalSearch(
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
    reviewer_runtime = ScriptedAgentRuntime([_EMPTY_DRAFT])
    events = _Events()
    runner, _answerer, _factory = _runner(
        plan=_plan(*tasks),
        query_runtime=ScriptedAgentRuntime([_query_draft([]) for _ in tasks]),
        reviewer_runtime=reviewer_runtime,
        external_gateway=_FakeExternalSearchGateway(),
        internal_search=internal_search,
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
