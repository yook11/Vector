"""AnsweringRunner: 内部+外部候補統合レビューのorchestration契約(D4-S1)。

保証するテスト条件 7 (内部のみ/外部のみでもreviewerへ進む)、8 (両方ゼロで
reviewer未呼び出し)、9 (合流でのcuration_id先勝ちdedupとsource_ref非衝突)、
11 (time filter失敗でもexternal runtime scopeがactivateされる)、および
reviewerの2 attempt失敗が兄弟taskの根拠を消さないこと(選別のtest contract
8番目)を、`AnsweringRunner.run()` を通した黒箱契約として検証する。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerOutcome,
    EvidenceAnswerUnavailable,
)
from app.agent.evidence_collection import NewsCollector, Researcher
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
    InternalSearchError,
)
from app.agent.evidence_review.contract import EvidenceReviewDraft
from app.agent.evidence_review.reviewer import EvidenceReviewer
from app.agent.planning.contract import (
    ExternalResearchTask,
    PlanningRequest,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.question_context import QuestionContext
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
_DEFAULT_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)


def _review_draft(
    selections: list[dict[str, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> EvidenceReviewDraft:
    return EvidenceReviewDraft.model_validate(
        {"selections": selections or [], "missing": missing or []}
    )


def _external_research_runtime(
    *,
    query_runtime: object,
    reviewer_runtime: object,
    tool: object,
) -> ExternalResearchRuntime:
    return ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        reviewer_runtime=reviewer_runtime,  # type: ignore[arg-type]
        search_tool=tool,  # type: ignore[arg-type]
    )


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal)


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


def _query_draft(queries: list[str]) -> Any:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalQueryDraft,
    )

    return ExternalQueryDraft(queries=queries)


def _external_candidate(
    url: str, *, title: str | None = None
) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet="snippet",
        source_name="Example",
        published_at=AS_OF,
    )


def _internal_hit(
    *,
    assessment_id: int,
    curation_id: int,
    title: str,
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id,
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


class _Preparer:
    async def prepare(self, **_kwargs: object) -> QuestionContext:
        return QuestionContext(standalone_question="NVIDIA の見通しは？")


class _Planner:
    def __init__(self, plan: SearchPlan) -> None:
        self._plan = plan

    async def plan(self, request: PlanningRequest) -> SearchPlan:
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
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerOutcome:
        del request, target_time_window, review_missing
        self.calls.append(list(evidence))
        if evidence:
            return EvidenceAnswerDraft(
                answer="根拠に基づく回答です。",
                cited_refs=[item.source.source_ref for item in evidence],
            )
        # 自己申告でinsufficientを名乗る形は無くなったため、
        # evidenceが無く回答を作れなかった場合はunavailableで表す。
        return EvidenceAnswerUnavailable(failure_code="fake_no_evidence")


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


async def _run(runner: AnsweringRunner) -> Any:
    return await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
    )


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
async def test_task_with_both_candidates_empty_skips_review() -> None:
    """保証するテスト条件 8。両方候補ゼロならreviewerを呼ばない。"""
    reviewer_runtime = ScriptedAgentRuntime([])
    runner, answerer, _factory = _runner(
        goals=["empty task"],
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=reviewer_runtime,
        tool=_ExternalTool({"q": []}),
        internal_tool=_InternalTool([[]]),
    )

    result = await _run(runner)

    assert reviewer_runtime.calls == []
    assert answerer.calls == [[]]
    assert result.final_output.status == "insufficient"


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


@pytest.mark.asyncio
async def test_reviewer_failure_after_two_attempts_empties_the_whole_run() -> None:
    """S1 E1(旧: 選別のtest contract 8番目を上書き)。

    段4時点はreviewerがtask単位だったため、片方が2 attempt失敗しても兄弟taskは
    根拠を返せた。S1でreviewerの呼び出しがRun単位1回になったため、精査の失敗は
    Run全体に及び、いずれのtaskも根拠を返さない
    (仕様「何ができていないかの表明」: 精査を通っていない候補を出典として提示しない)。

    scriptは[失敗, 失敗, 成功draft](REVISE 1)。Run単位1回なら2 attemptで打ち切られ
    成功draftへ届かないが、per-task実装ならtask0の2失敗を消費した後にtask1が
    新しいattempt列として成功draftを消費し根拠を返してしまうため、
    [失敗]*Nのように常に失敗するscriptでは区別できない不変条件を判別できる。
    """
    internal_tool = _InternalTool(
        [
            [
                _internal_hit(
                    assessment_id=1001, curation_id=1, title="failing task hit"
                )
            ],
            [_internal_hit(assessment_id=1002, curation_id=2, title="succeeding hit")],
        ]
    )
    failure = AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    success_draft = _review_draft(
        [{"candidate_index": 0, "claim": "claim", "why_selected": "w"}]
    )
    reviewer_runtime = ScriptedAgentRuntime([failure, failure, success_draft])
    runner, answerer, _factory = _runner(
        goals=["failing task", "succeeding task"],
        query_runtime=ScriptedAgentRuntime([_query_draft([]), _query_draft([])]),
        reviewer_runtime=reviewer_runtime,
        tool=_ExternalTool(),
        internal_tool=internal_tool,
    )

    await _run(runner)

    assert answerer.calls == [[]]
