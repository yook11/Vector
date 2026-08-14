"""AnsweringRunner: 内部+外部候補統合レビューのorchestration契約(D4-S1)。

保証するテスト条件 7 (内部のみ/外部のみでもreviewerへ進む)、9 (合流での
curation_id先勝ちdedup)、11 (time filter失敗でもexternal runtime scopeが
activateされる)を `AnsweringRunner.run()` を通した黒箱契約として検証する。
候補ゼロのskipとreviewer失敗の正本は test_evidence_review_run_scope.py。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

from app.agent.evidence_collection import NewsCollector, Researcher
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
    InternalSearchError,
)
from app.agent.evidence_review.reviewer import EvidenceReviewer
from app.agent.planning.contract import (
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.running import AnsweringPhases, AnsweringRunner
from tests.agent.running._harness import (
    DEFAULT_TARGET_TIME_WINDOW as _DEFAULT_TARGET_TIME_WINDOW,
)
from tests.agent.running._harness import (
    EvidenceAnswerer as _EvidenceAnswerer,
)
from tests.agent.running._harness import (
    ExternalSearchTool as _ExternalTool,
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
    execute_run as _run,
)
from tests.agent.running._harness import (
    external_candidate as _external_candidate,
)
from tests.agent.running._harness import (
    external_research_runtime as _external_research_runtime,
)
from tests.agent.running._harness import (
    internal_hit as _internal_hit,
)
from tests.agent.running._harness import (
    query_draft as _query_draft,
)
from tests.agent.running._harness import (
    review_draft as _review_draft,
)
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime


def _plan(
    goals: list[str],
    *,
    target_time_window: TargetTimeWindow | None = _DEFAULT_TARGET_TIME_WINDOW,
) -> SearchPlan:
    return SearchPlan(
        research_tasks=[
            ResearchTask(research_goal=goal, article_search_queries=["query"])
            for goal in goals
        ],
        target_time_window=target_time_window,
    )


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


class _InternalTool:
    def __init__(
        self,
        hits_by_call: list[list[InternalArticleSearchHit]],
        *,
        error: BaseException | None = None,
    ) -> None:
        self._hits_by_call = list(hits_by_call)
        self._error = error
        self.calls: list[Any] = []

    @property
    def name(self) -> str:
        return "internal_search"

    async def invoke(self, input: Any) -> list[InternalArticleSearchHit]:
        self.calls.append(input)
        if self._error is not None:
            raise self._error
        if not self._hits_by_call:
            return []
        return self._hits_by_call.pop(0)


def _runner(
    *,
    goals: list[str],
    query_runtime: object,
    reviewer_runtime: object,
    tool: object,
    internal_tool: _InternalTool,
    target_time_window: TargetTimeWindow | None = _DEFAULT_TARGET_TIME_WINDOW,
) -> tuple[AnsweringRunner, _EvidenceAnswerer, _Factory]:
    answerer = _EvidenceAnswerer()
    runtime = _external_research_runtime(
        query_runtime=query_runtime,
        reviewer_runtime=reviewer_runtime,
        tool=tool,
    )
    factory = _Factory(runtime)
    phases = AnsweringPhases(
        planner=_Planner(_plan(goals, target_time_window=target_time_window)),
        collector=NewsCollector(
            researcher=Researcher(internal_search=internal_tool),
            requested_agent_count=1,
        ),
        reviewer=EvidenceReviewer(),
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )
    return runner, answerer, factory


@pytest.mark.asyncio
async def test_task_with_only_internal_candidates_still_reaches_review() -> None:
    """保証するテスト条件 7(内部のみ)。外部全滅でも精査へ進み内部根拠を返す。"""
    internal_tool = _InternalTool(
        [[_internal_hit(assessment_id=1001, curation_id=1, title="internal only")]]
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    runner, answerer, _factory = _runner(
        goals=["internal only task"],
        query_runtime=ScriptedAgentRuntime([_query_draft([])]),
        reviewer_runtime=reviewer_runtime,
        tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    result = await _run(runner)

    assert len(reviewer_runtime.calls) == 1
    assert [item.source.title for item in answerer.calls[0]] == ["internal only"]
    assert result.final_output.status == "answered"


@pytest.mark.asyncio
async def test_task_with_only_external_candidates_still_reaches_review() -> None:
    """保証するテスト条件 7(外部のみ)。内部失敗でも精査へ進み外部根拠を返す。"""
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    runner, answerer, _factory = _runner(
        goals=["external only task"],
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=reviewer_runtime,
        tool=_ExternalTool({"q": [_external_candidate("https://example.com/only")]}),
        internal_tool=_InternalTool(
            [], error=InternalSearchError(phase="article_search")
        ),
    )

    result = await _run(runner)

    assert len(reviewer_runtime.calls) == 1
    assert [item.source.title for item in answerer.calls[0]] == ["only"]
    # D4-S2: run単位のcollection_failuresは廃止された。このtaskはreview=
    # succeeded(外部候補を精査して採用)で完了扱いになるため、内部収集が
    # 失敗していてもstatusはansweredになる。
    assert result.final_output.status == "answered"


@pytest.mark.asyncio
async def test_merge_dedupes_same_internal_article_by_curation_id_first_win() -> None:
    """保証するテスト条件 9(S1 C3: この不変条件はRun単位化でも変更されない)。

    同じ内部記事を複数taskが採用したときcuration_id先勝ち。S1でreviewer呼び出しが
    Run単位1回になっても、統合index空間で両taskの候補を選ばせれば同じ結果になる
    必要があるため、1回の呼び出しを前提にscriptを組む(taskごと呼び出しが残る間は
    余剰callへ空draftを充ててcrashを避ける)。
    """
    shared_curation_id = 42
    internal_tool = _InternalTool(
        [
            [
                _internal_hit(
                    assessment_id=1001,
                    curation_id=shared_curation_id,
                    title="shared article (task0)",
                )
            ],
            [
                _internal_hit(
                    assessment_id=1002,
                    curation_id=shared_curation_id,
                    title="shared article (task1)",
                )
            ],
        ]
    )
    # 統合index空間(仮定: task昇順で結合): task0の唯一の候補が0、task1が1。
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _review_draft(
                [
                    {"candidate_index": 0, "claim": "first claim", "why_selected": "w"},
                    {
                        "candidate_index": 1,
                        "claim": "second claim",
                        "why_selected": "w",
                    },
                ]
            ),
            _review_draft([]),
        ]
    )
    runner, answerer, _factory = _runner(
        goals=["first task", "second task"],
        query_runtime=ScriptedAgentRuntime([_query_draft([]), _query_draft([])]),
        reviewer_runtime=reviewer_runtime,
        tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    await _run(runner)

    # source_ref の task間非衝突は f"{task_index}-{candidate_index}" という統合index
    # 空間の形から構造的に保証される(正本:
    # tests/agent/evidence_review/test_policy.py の
    # test_build_evidence_assigns_task_scoped_source_ref_without_origin_prefix)。
    # ここでは合流のcuration_id先勝ちdedupだけを検証する。
    titles = [item.source.title for item in answerer.calls[0]]
    assert titles == ["shared article (task0)"]


@pytest.mark.asyncio
async def test_time_filter_failure_still_activates_external_scope_for_review() -> None:
    """保証するテスト条件 11。time filter失敗でもreviewerのLLM runtimeが使えるよう
    external runtime scopeがactivateされる(direct pathの非activateは不変)。
    """
    internal_tool = _InternalTool(
        [[_internal_hit(assessment_id=1001, curation_id=1, title="internal candidate")]]
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    runner, answerer, factory = _runner(
        goals=["closed by time filter"],
        query_runtime=ScriptedAgentRuntime([]),
        reviewer_runtime=reviewer_runtime,
        tool=_ExternalTool(),
        internal_tool=internal_tool,
        target_time_window=TargetTimeWindow(kind="unsupported_explicit_window"),
    )

    await _run(runner)

    assert len(factory.scopes) == 1
    assert factory.scopes[0].entered is True
    assert factory.scopes[0].exit_calls == 1
    assert len(reviewer_runtime.calls) == 1
    assert [item.source.title for item in answerer.calls[0]] == ["internal candidate"]
