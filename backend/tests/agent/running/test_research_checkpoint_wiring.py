"""RunResult.research_checkpoint への runner 配線契約(agent-research-checkpoint
-context-slice の「記録フロー」5番)。

build_research_checkpoint()自体の詰め替え規則はtests/agent/research_checkpoint/
test_builder.pyが正本。ここではAnsweringRunner.run()を通した黒箱として、
SearchPlan成功時にRunResultへ運ばれること、direct_answer/review失敗/
review skip/builder例外でNoneのまま回答が継続することだけを検証する。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerOutcome,
)
from app.agent.evidence_collection import NewsCollector, Researcher
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalResearchRuntime,
    ExternalSearchHit,
    ExternalSearchProviderError,
    ExternalSearchToolFailureReason,
)
from app.agent.evidence_review import EvidenceReviewer
from app.agent.evidence_review.selection import EvidenceReviewerDraft
from app.agent.planning.contract import (
    DirectAnswerPlan,
    PlanningRequest,
    QuestionPlan,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.question_context import QuestionContext
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197660")
AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)


def _plan(*, research_goal: str) -> SearchPlan:
    return SearchPlan(
        research_tasks=[
            ResearchTask(research_goal=research_goal, article_search_queries=["seed"])
        ],
        target_time_window=_TARGET_TIME_WINDOW,
    )


def _query_draft(queries: list[str]) -> ExternalQueryDraft:
    return ExternalQueryDraft(queries=queries)


def _external_hit(url: str) -> ExternalSearchHit:
    return ExternalSearchHit(
        url=url,
        title=url.rsplit("/", maxsplit=1)[-1],
        snippet="snippet",
        source_name="Example",
        published_at=AS_OF,
    )


def _review_draft(
    selections: list[dict[str, object]], *, missing: list[str] | None = None
) -> EvidenceReviewerDraft:
    return EvidenceReviewerDraft.model_validate(
        {"selections": selections, "missing": missing or []}
    )


class _Preparer:
    async def prepare(self, **_kwargs: object) -> QuestionContext:
        return QuestionContext(standalone_question="質問")


class _Planner:
    def __init__(self, plan: QuestionPlan) -> None:
        self._plan = plan

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        del request
        return self._plan


class _DirectAnswerer:
    async def answer(
        self, *, request: AnsweringRequest, previous_answer: str = ""
    ) -> DirectAnswerDraft:
        del request, previous_answer
        return DirectAnswerDraft(answer="直接回答")


class _UnreachableDirectAnswerer:
    async def answer(
        self, *, request: AnsweringRequest, previous_answer: str = ""
    ) -> DirectAnswerDraft:
        raise AssertionError(
            f"direct answerer must not run: {request!r} {previous_answer!r}"
        )


class _UnreachableEvidenceAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerOutcome:
        raise AssertionError(
            f"evidence answerer must not run: {request!r} {evidence!r} "
            f"{target_time_window!r} {review_missing!r}"
        )


class _EvidenceAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerOutcome:
        del request, target_time_window, review_missing
        return EvidenceAnswerDraft(
            answer="根拠に基づく回答です。",
            cited_refs=[item.source.source_ref for item in evidence],  # type: ignore[attr-defined]
        )


class _UnreachableRuntimeFactory:
    def activate(self) -> object:
        raise AssertionError("external runtime must not activate")


class _InternalTool:
    @property
    def name(self) -> str:
        return "internal_search"

    async def search(self, input: object) -> list[object]:
        del input
        return []


class _ExternalTool:
    """queryごとにhitまたはprovider失敗を返すfake。"""

    def __init__(
        self,
        results_by_query: dict[str, list[ExternalSearchHit]],
        *,
        failing_queries: tuple[str, ...] = (),
    ) -> None:
        self._results = results_by_query
        self._failing = set(failing_queries)
        self.calls: list[object] = []

    @property
    def name(self) -> str:
        return "external_search"

    async def search(self, input: object) -> list[ExternalSearchHit]:
        self.calls.append(input)
        query = input.query  # type: ignore[attr-defined]
        if query in self._failing:
            raise ExternalSearchProviderError(
                reason=ExternalSearchToolFailureReason.HTTP_ERROR
            )
        return list(self._results.get(query, []))


class _RuntimeFactory:
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime

    @asynccontextmanager
    async def activate(self):
        yield self._runtime


def _search_runner(
    *,
    plan: SearchPlan,
    query_runtime: ScriptedAgentRuntime,
    reviewer_runtime: ScriptedAgentRuntime,
    tool: _ExternalTool,
) -> AnsweringRunner:
    phases = AnsweringPhases(
        planner=_Planner(plan),
        collector=NewsCollector(
            researcher=Researcher(internal_search=_InternalTool()),
            requested_agent_count=1,
        ),
        reviewer=EvidenceReviewer(),
        external_runtime_factory=_RuntimeFactory(
            ExternalResearchRuntime(
                query_runtime=query_runtime,  # type: ignore[arg-type]
                reviewer_runtime=reviewer_runtime,  # type: ignore[arg-type]
                search_tool=tool,  # type: ignore[arg-type]
            )
        ),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=_EvidenceAnswerer(),
    )
    return AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )


async def _run(runner: AnsweringRunner) -> object:
    return await runner.run(
        RunInput(question="質問", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
    )


async def test_search_plan_success_populates_checkpoint_from_review_outcome() -> None:
    """記録フロー2・5: evidence review成功時、planのresearch_goal・provider成功
    queryだけの実行query・外部採用claim・Reviewer missingがcheckpointへ運ばれる。
    """
    goal = "NVIDIA の直近発表を調べる"
    tool = _ExternalTool(
        results_by_query={"q-ok": [_external_hit("https://example.com/a")]},
        failing_queries=("q-fail",),
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _review_draft(
                [
                    {
                        "option_index": 0,
                        "claim": "採用された事実",
                        "why_selected": "w",
                    }
                ],
                missing=["未確認事項"],
            )
        ]
    )
    runner = _search_runner(
        plan=_plan(research_goal=goal),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q-ok", "q-fail"])]),
        reviewer_runtime=reviewer_runtime,
        tool=tool,
    )

    result = await _run(runner)

    checkpoint = result.research_checkpoint  # type: ignore[attr-defined]
    assert checkpoint is not None
    assert checkpoint.as_of == AS_OF
    assert len(checkpoint.tasks) == 1
    task = checkpoint.tasks[0]
    assert task.research_goal == goal
    # provider失敗したq-failは記録されず、成功したq-okだけが残る。
    assert task.executed_queries == ("q-ok",)
    assert task.adopted_claims == ("採用された事実",)
    assert checkpoint.unresolved_after_search == ("未確認事項",)


async def test_direct_answer_plan_leaves_checkpoint_none() -> None:
    """記録フロー6: 外部検索を実行しないdirect_answer Runはcheckpointを持たない。"""
    phases = AnsweringPhases(
        planner=_Planner(DirectAnswerPlan()),
        collector=NewsCollector(researcher=Researcher(internal_search=_InternalTool())),
        reviewer=EvidenceReviewer(),
        external_runtime_factory=_UnreachableRuntimeFactory(),
        direct_answerer=_DirectAnswerer(),
        evidence_answerer=_UnreachableEvidenceAnswerer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )

    result = await _run(runner)

    assert result.research_checkpoint is None  # type: ignore[attr-defined]


async def test_evidence_review_failure_leaves_checkpoint_none() -> None:
    """記録フロー4: reviewerが2 attempt失敗した場合、checkpointを組み立てない。"""
    tool = _ExternalTool(
        results_by_query={"q": [_external_hit("https://example.com/a")]}
    )
    failure = AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    runner = _search_runner(
        plan=_plan(research_goal="調査目標"),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=ScriptedAgentRuntime([failure, failure]),
        tool=tool,
    )

    result = await _run(runner)

    assert result.research_checkpoint is None  # type: ignore[attr-defined]


async def test_skipped_empty_review_records_executed_query_with_no_adopted_claims() -> (
    None
):
    """記録フロー3: 内外のヒットともゼロでreviewerが呼ばれない(skipped_empty)場合も、

    provider呼び出しに成功したtaskは空adopted_claims・空unresolved_after_searchで
    記録される(review失敗経路と異なりNoneにはならない)。
    """
    tool = _ExternalTool(results_by_query={})
    reviewer_runtime = ScriptedAgentRuntime([])
    runner = _search_runner(
        plan=_plan(research_goal="調査目標"),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=reviewer_runtime,
        tool=tool,
    )

    result = await _run(runner)

    checkpoint = result.research_checkpoint  # type: ignore[attr-defined]
    assert checkpoint is not None
    assert len(checkpoint.tasks) == 1
    assert (
        checkpoint.tasks[0].research_goal,
        checkpoint.tasks[0].executed_queries,
        checkpoint.tasks[0].adopted_claims,
        checkpoint.unresolved_after_search,
    ) == ("調査目標", ("q",), (), ())
    assert reviewer_runtime.calls == []


async def test_builder_exception_yields_none_checkpoint_and_continues_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """記録フロー4: 組み立て失敗はcomplete_run呼び出し前に閉じ、回答は継続する。"""

    def _raise_build_failure(**_kwargs: object) -> None:
        raise RuntimeError("checkpoint build boom")

    monkeypatch.setattr(
        "app.agent.research_checkpoint.builder.build_research_checkpoint",
        _raise_build_failure,
    )
    tool = _ExternalTool(
        results_by_query={"q": [_external_hit("https://example.com/a")]}
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft([{"option_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    runner = _search_runner(
        plan=_plan(research_goal="調査目標"),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=reviewer_runtime,
        tool=tool,
    )

    result = await _run(runner)

    assert result.research_checkpoint is None  # type: ignore[attr-defined]
    assert result.final_output.status == "answered"  # type: ignore[attr-defined]
