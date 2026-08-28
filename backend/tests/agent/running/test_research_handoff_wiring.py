"""RunResult.research_handoff への runner 配線契約。

台帳の詰め替え規則はtests/agent/research_handoff/test_builder.py、整理の
書き直し規則は同test_service.pyが正本。ここではAnsweringRunner.run()を通した
黒箱として、台帳と整理がRunResultへ運ばれること、申し送りを触らない経路で
Noneのまま回答が継続すること、整理が間に合わなくても台帳が残ることを検証する。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
)
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerInput,
    EvidenceAnswerOutcome,
)
from app.agent.contract import AnswerGenerationStopped
from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search import ExternalSearchService
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalSearchFailureReason,
    ExternalSearchHit,
    ExternalSearchProviderError,
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
from app.agent.research_handoff import ResearchHandoff, ResearchHandoffInput
from app.agent.running import AnsweringPhases, AnsweringRunner, RunInput
from app.agent.running import answering_runner as answering_runner_module
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from tests.agent.running._harness import (
    PassThroughOrganizer,
    fixed_scope,
    run_identity,
)
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
        content="content",
        source_name="Example",
        published_at=AS_OF,
    )


def _review_draft(
    selections: list[dict[str, object]], *, missing: list[str] | None = None
) -> EvidenceReviewerDraft:
    return EvidenceReviewerDraft.model_validate(
        {"selections": selections, "missing": missing or []}
    )


class _Planner:
    def __init__(self, plan: QuestionPlan) -> None:
        self._plan = plan

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        del request
        return self._plan


class _DirectAnswerer:
    async def answer(self, input: DirectAnswerInput) -> DirectAnswerDraft:
        del input
        return DirectAnswerDraft(answer="直接回答")


class _UnreachableDirectAnswerer:
    async def answer(self, input: DirectAnswerInput) -> DirectAnswerDraft:
        raise AssertionError(f"direct answerer must not run: {input!r}")


class _UnreachableEvidenceAnswerer:
    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerOutcome:
        raise AssertionError(f"evidence answerer must not run: {input!r}")


class _EvidenceAnswerer:
    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerOutcome:
        return EvidenceAnswerDraft(
            answer="根拠に基づく回答です。",
            cited_refs=[item.source.source_ref for item in input.evidence],
        )


class _UnreachableScope:
    def __call__(self) -> object:
        raise AssertionError("external scope must not activate")


class _FakeInternalSearch:
    async def search(self, queries: object) -> list[object]:
        del queries
        return []


class _FakeExternalSearchGateway:
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

    async def search(self, request: object) -> list[ExternalSearchHit]:
        self.calls.append(request)
        query = request.query  # type: ignore[attr-defined]
        if query in self._failing:
            raise ExternalSearchProviderError(
                reason=ExternalSearchFailureReason.HTTP_ERROR
            )
        return list(self._results.get(query, []))


def _search_runner(
    *,
    plan: SearchPlan,
    query_runtime: ScriptedAgentRuntime,
    reviewer_runtime: ScriptedAgentRuntime,
    gateway: _FakeExternalSearchGateway,
    organizer: object | None = None,
    evidence_answerer: object | None = None,
) -> AnsweringRunner:
    phases = AnsweringPhases(
        planner=_Planner(plan),
        collector=EvidenceCollectionService(
            internal_search=_FakeInternalSearch(),
            external_search_scope_factory=fixed_scope(
                ExternalSearchService(
                    query_runtime=query_runtime,  # type: ignore[arg-type]
                    search_gateway=gateway,  # type: ignore[arg-type]
                )
            ),
        ),
        reviewer=EvidenceReviewer(runtime_scope_factory=fixed_scope(reviewer_runtime)),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=evidence_answerer or _EvidenceAnswerer(),  # type: ignore[arg-type]
        organizer=organizer or PassThroughOrganizer(),  # type: ignore[arg-type]
    )
    return AnsweringRunner(
        phases_factory=lambda: phases,
    )


async def _run(runner: AnsweringRunner) -> object:
    return await runner.run(
        RunInput(question="質問", history=()),
        identity=run_identity(run_id=RUN_ID, as_of=AS_OF),
    )


async def test_search_plan_records_goal_and_queries_that_reached_a_provider() -> None:
    """台帳にはplanのresearch_goalと、provider呼び出しに成功したqueryだけが載る。"""
    goal = "NVIDIA の直近発表を調べる"
    gateway = _FakeExternalSearchGateway(
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
        gateway=gateway,
    )

    result = await _run(runner)

    handoff = result.research_handoff  # type: ignore[attr-defined]
    assert handoff is not None
    assert len(handoff.runs) == 1
    record = handoff.runs[0]
    assert handoff.updated_at == AS_OF
    assert record.as_of == AS_OF
    assert len(record.tasks) == 1
    task = record.tasks[0]
    assert task.research_goal == goal
    # provider失敗したq-failは記録されず、成功したq-okだけが残る。
    assert task.executed_queries == ("q-ok",)


async def test_direct_answer_plan_leaves_handoff_none() -> None:
    """外部検索を実行しないdirect_answer Runはhandoffを触らない。"""
    phases = AnsweringPhases(
        planner=_Planner(DirectAnswerPlan()),
        collector=EvidenceCollectionService(
            internal_search=_FakeInternalSearch(),
            external_search_scope_factory=_UnreachableScope(),
        ),
        reviewer=EvidenceReviewer(runtime_scope_factory=_UnreachableScope()),
        direct_answerer=_DirectAnswerer(),
        evidence_answerer=_UnreachableEvidenceAnswerer(),
        organizer=PassThroughOrganizer(),
    )
    runner = AnsweringRunner(
        phases_factory=lambda: phases,
    )

    result = await _run(runner)

    assert result.research_handoff is None  # type: ignore[attr-defined]


async def test_evidence_review_failure_leaves_handoff_none() -> None:
    """reviewerが2 attempt失敗した場合、記録を組み立てない。"""
    gateway = _FakeExternalSearchGateway(
        results_by_query={"q": [_external_hit("https://example.com/a")]}
    )
    failure = AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    runner = _search_runner(
        plan=_plan(research_goal="調査目標"),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=ScriptedAgentRuntime([failure, failure]),
        gateway=gateway,
    )

    result = await _run(runner)

    assert result.research_handoff is None  # type: ignore[attr-defined]


async def test_a_run_that_found_nothing_still_records_the_query_it_executed() -> None:
    """内外のヒットがゼロでreviewerを呼ばない場合も、叩いたqueryは台帳に残る。

    次のRunが同じqueryを繰り返さないために要る。review失敗経路と違いNoneにならない。
    """
    gateway = _FakeExternalSearchGateway(results_by_query={})
    reviewer_runtime = ScriptedAgentRuntime([])
    runner = _search_runner(
        plan=_plan(research_goal="調査目標"),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=reviewer_runtime,
        gateway=gateway,
    )

    result = await _run(runner)

    handoff = result.research_handoff  # type: ignore[attr-defined]
    assert handoff is not None
    record = handoff.runs[0]
    assert len(record.tasks) == 1
    assert (
        record.tasks[0].research_goal,
        record.tasks[0].executed_queries,
    ) == ("調査目標", ("q",))
    assert reviewer_runtime.calls == []


async def test_builder_exception_yields_none_handoff_and_continues_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """記録フロー4: 組み立て失敗はcomplete_run呼び出し前に閉じ、回答は継続する。"""

    def _raise_build_failure(**_kwargs: object) -> None:
        raise RuntimeError("run record build boom")

    monkeypatch.setattr(
        "app.agent.research_handoff.handoff_input._build_research_run_record",
        _raise_build_failure,
    )
    gateway = _FakeExternalSearchGateway(
        results_by_query={"q": [_external_hit("https://example.com/a")]}
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft([{"option_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    runner = _search_runner(
        plan=_plan(research_goal="調査目標"),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=reviewer_runtime,
        gateway=gateway,
    )

    result = await _run(runner)

    assert result.research_handoff is None  # type: ignore[attr-defined]
    assert result.final_output.status == "answered"  # type: ignore[attr-defined]


class _StoppingEvidenceAnswerer:
    """streamingの途中で停止する回答工程。並行taskへ一度制御を渡してから止まる。"""

    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerOutcome:
        del input
        await asyncio.sleep(0)
        raise AnswerGenerationStopped()


class _Organizer:
    """整理3本を書き込む、または指定の例外を送出するorganizer。"""

    def __init__(self, *, outcome: Exception | str = "整理済み") -> None:
        self._outcome = outcome
        self.calls: list[ResearchHandoffInput] = []

    async def organize(self, input: ResearchHandoffInput) -> ResearchHandoff:
        self.calls.append(input)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return input.handoff.model_copy(update={"collected_overview": self._outcome})


class _NeverFinishingOrganizer:
    """待ち合わせが打ち切られるまで終わらないorganizer。"""

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def organize(self, input: ResearchHandoffInput) -> ResearchHandoff:
        del input
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


def _searching_runner(organizer: object) -> AnsweringRunner:
    return _search_runner(
        plan=_plan(research_goal="調査目標"),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=ScriptedAgentRuntime([_review_draft([])]),
        gateway=_FakeExternalSearchGateway(
            results_by_query={"q": [_external_hit("https://example.com/a")]}
        ),
        organizer=organizer,
    )


async def test_organized_text_reaches_the_run_result_with_the_ledger() -> None:
    """整理は台帳と同じhandoffに乗って1トランザクションで確定できる形で返る。"""
    organizer = _Organizer()

    result = await _run(_searching_runner(organizer))

    handoff = result.research_handoff  # type: ignore[attr-defined]
    assert handoff is not None
    assert handoff.collected_overview == "整理済み"
    assert len(handoff.runs) == 1


async def test_organizer_sees_what_was_searched_and_what_was_collected() -> None:
    """整理の素材には、叩いたqueryと集まった記事の見出しが渡る。"""
    organizer = _Organizer()

    await _run(_searching_runner(organizer))

    assert len(organizer.calls) == 1
    organizer_input = organizer.calls[0]
    assert organizer_input.question == "質問"
    assert [task.executed_queries for task in organizer_input.tasks] == [("q",)]
    assert [task.hit_headlines for task in organizer_input.tasks] == [("a",)]


async def test_a_failing_organizer_still_lets_the_run_complete_with_the_ledger() -> (
    None
):
    """回答は確定済みなので、整理が壊れてもRunを失敗させず台帳だけ残す。"""
    result = await _run(_searching_runner(_Organizer(outcome=RuntimeError("boom"))))

    handoff = result.research_handoff  # type: ignore[attr-defined]
    assert handoff is not None
    assert handoff.collected_overview == ""
    assert len(handoff.runs) == 1


async def test_an_organizer_that_never_finishes_is_cut_off_and_the_ledger_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """待ち合わせを打ち切っても台帳は残り、並行taskはcancelされて合流する。"""
    monkeypatch.setattr(
        answering_runner_module, "HANDOFF_ORGANIZE_TIMEOUT_SECONDS", 0.01
    )
    organizer = _NeverFinishingOrganizer()

    result = await _run(_searching_runner(organizer))

    handoff = result.research_handoff  # type: ignore[attr-defined]
    assert handoff is not None
    assert handoff.collected_overview == ""
    assert len(handoff.runs) == 1
    assert organizer.cancelled.is_set()


async def test_stopping_the_answer_cancels_the_organizer_and_writes_nothing() -> None:
    """停止したRunはRunResultを返さないため、申し送りは書かれない。

    並行taskはRunを抜ける前にcancelして合流し、Runの外へ残さない。
    """
    organizer = _NeverFinishingOrganizer()
    runner = _search_runner(
        plan=_plan(research_goal="調査目標"),
        query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
        reviewer_runtime=ScriptedAgentRuntime([_review_draft([])]),
        gateway=_FakeExternalSearchGateway(
            results_by_query={"q": [_external_hit("https://example.com/a")]}
        ),
        organizer=organizer,
        evidence_answerer=_StoppingEvidenceAnswerer(),
    )

    with pytest.raises(AnswerGenerationStopped):
        await _run(runner)

    assert organizer.cancelled.is_set()
