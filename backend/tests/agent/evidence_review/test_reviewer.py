"""EvidenceReviewer.review() の単体契約テスト(S1: Run単位1回)。

reviewerはRun内の全taskの候補を1回の入力で受け取り、Run全体としての採用と
不足を1つの出力で返す(仕様「Run単位で精査する」)。attempt/timeout/失敗分類の
規則は段4時点から変わらないが、適用範囲がtaskからRunへ広がる。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.agent.evidence_collection.contract import CollectedTask, ResearchTaskReport
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_review.contract import EvidenceReviewDraft
from app.agent.evidence_review.reviewer import EvidenceReviewer
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderError, AIProviderNetworkError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from tests.agent.runtime._fakes import ScriptedAgentRuntime

_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _collected_task(
    *,
    task_index: int,
    research_goal: str = "NVIDIA の最新動向を確認する",
    internal_hits: list[InternalArticleSearchHit] | None = None,
    external_candidates: list[ExternalSearchCandidate] | None = None,
) -> CollectedTask:
    hits = internal_hits or []
    candidates = external_candidates or []
    # report は reviewer が読まない収集診断のため、成功形の最小値で埋める。
    return CollectedTask(
        task_index=task_index,
        research_goal=research_goal,
        internal_hits=hits,
        external_candidates=candidates,
        executed_queries=(),
        report=ResearchTaskReport(
            task_index=task_index,
            research_goal=research_goal,
            internal_collection="succeeded",
            external_collection="succeeded",
            internal_candidate_count=len(hits),
            external_candidate_count=len(candidates),
        ),
    )


def _internal_hit(
    *,
    assessment_id: int = 1001,
    curation_id: int = 1,
    title: str = "internal title",
    summary: str = "internal summary",
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id,
        title=title,
        summary=summary,
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


def _external_candidate(
    url: str = "https://example.com/a", *, title: str | None = None
) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(
        url=url,
        title=title or url,
        snippet="snippet",
        source_name="Example",
        published_at=_AS_OF,
    )


def _draft(
    selections: list[dict[str, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> EvidenceReviewDraft:
    return EvidenceReviewDraft.model_validate(
        {"selections": selections or [], "missing": missing or []}
    )


async def _review(
    *,
    tasks: list[Any],
    as_of: datetime = _AS_OF,
    reviewer_runtime: Any,
) -> Any:
    reviewer = EvidenceReviewer()
    return await reviewer.review(
        tasks=tasks,
        as_of=as_of,
        reviewer_runtime=reviewer_runtime,
    )


@pytest.mark.asyncio
async def test_review_is_called_exactly_once_for_a_multi_task_run() -> None:
    """S1 A1。複数taskがあってもreviewer_runtime.invokeは1 attemptにつき1回。"""
    runtime = ScriptedAgentRuntime(
        [_draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    tasks = [
        _collected_task(task_index=0, internal_hits=[_internal_hit()]),
        _collected_task(task_index=1, external_candidates=[_external_candidate()]),
    ]

    await _review(tasks=tasks, reviewer_runtime=runtime)

    assert len(runtime.calls) == 1


@pytest.mark.asyncio
async def test_successful_review_resolves_the_originating_task_index() -> None:
    """S1: 採用された根拠のtask_indexは、その候補が属するtaskの値になる。"""
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                [{"candidate_index": 0, "claim": "internal claim", "why_selected": "w"}]
            )
        ]
    )

    outcome = await _review(
        tasks=[_collected_task(task_index=3, internal_hits=[_internal_hit()])],
        reviewer_runtime=runtime,
    )

    assert outcome.failure_reason is None
    assert len(outcome.internal_evidence) == 1
    assert outcome.internal_evidence[0].claim == "internal claim"
    assert outcome.internal_evidence[0].task_index == 3
    assert outcome.external_evidence == []


@pytest.mark.asyncio
async def test_review_groups_candidates_by_task_in_ascending_task_index_order() -> None:
    """S1(候補の渡し方)。research_goalごとにグループ化しtask_index昇順で並べる。"""
    runtime = ScriptedAgentRuntime([_draft([])])
    tasks = [
        _collected_task(
            task_index=0,
            research_goal="goal-A",
            internal_hits=[_internal_hit(title="A-int")],
        ),
        _collected_task(
            task_index=1,
            research_goal="goal-B",
            external_candidates=[_external_candidate(title="B-ext")],
        ),
    ]

    await _review(tasks=tasks, reviewer_runtime=runtime)

    review_input = runtime.calls[0].input
    assert [group.task_index for group in review_input.task_groups] == [0, 1]
    assert [group.research_goal for group in review_input.task_groups] == [
        "goal-A",
        "goal-B",
    ]


@pytest.mark.asyncio
async def test_review_assigns_a_run_wide_index_internal_before_external_per_group() -> (
    None
):
    """S1(候補の渡し方)。indexはグループをまたぐ通し番号、group内は内部→外部。"""
    runtime = ScriptedAgentRuntime([_draft([])])
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(assessment_id=1001, curation_id=1, title="A-int-1"),
                _internal_hit(assessment_id=1002, curation_id=2, title="A-int-2"),
            ],
            external_candidates=[_external_candidate(title="A-ext-1")],
        ),
        _collected_task(
            task_index=1,
            internal_hits=[
                _internal_hit(assessment_id=1003, curation_id=3, title="B-int-1")
            ],
        ),
    ]

    await _review(tasks=tasks, reviewer_runtime=runtime)

    review_input = runtime.calls[0].input
    ordered = [
        (candidate.index, candidate.title)
        for group in review_input.task_groups
        for candidate in group.candidates
    ]
    assert ordered == [
        (0, "A-int-1"),
        (1, "A-int-2"),
        (2, "A-ext-1"),
        (3, "B-int-1"),
    ]


@pytest.mark.asyncio
async def test_selection_restores_candidate_and_task_from_a_cross_task_index() -> None:
    """S1(選別結果の復元)。グループをまたいだindexから候補と所属taskが復元される。"""
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(assessment_id=1001, curation_id=1, title="A-int-1"),
                _internal_hit(assessment_id=1002, curation_id=2, title="A-int-2"),
            ],
        ),
        _collected_task(
            task_index=1,
            external_candidates=[
                _external_candidate("https://example.com/b1", title="B-ext-1"),
                _external_candidate("https://example.com/b2", title="B-ext-2"),
            ],
        ),
    ]
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                [
                    {
                        "candidate_index": 1,
                        "claim": "A-int-2 claim",
                        "why_selected": "w",
                    },
                    {
                        "candidate_index": 3,
                        "claim": "B-ext-2 claim",
                        "why_selected": "w",
                    },
                ]
            )
        ]
    )

    outcome = await _review(tasks=tasks, reviewer_runtime=runtime)

    assert [(item.title, item.task_index) for item in outcome.internal_evidence] == [
        ("A-int-2", 0)
    ]
    assert [(item.title, item.task_index) for item in outcome.external_evidence] == [
        ("B-ext-2", 1)
    ]
    assert outcome.internal_evidence[0].source_ref == "0-1"
    assert outcome.external_evidence[0].source_ref == "1-3"


@pytest.mark.asyncio
async def test_review_completes_when_only_some_tasks_have_candidates() -> None:
    """S1 A1系。候補ゼロのtaskがあってもRun全体としての精査は1回で完了する。"""
    runtime = ScriptedAgentRuntime(
        [_draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}])]
    )
    tasks = [
        _collected_task(task_index=0, internal_hits=[_internal_hit()]),
        _collected_task(task_index=1),
    ]

    outcome = await _review(tasks=tasks, reviewer_runtime=runtime)

    assert outcome.failure_reason is None
    assert len(runtime.calls) == 1


@pytest.mark.asyncio
async def test_review_propagates_missing_as_a_single_run_level_list() -> None:
    """S1(何ができていないかの表明)。missingはRun全体で1本として返る。"""
    runtime = ScriptedAgentRuntime([_draft([], missing=["run全体の不足"])])

    outcome = await _review(
        tasks=[_collected_task(task_index=0, internal_hits=[_internal_hit()])],
        reviewer_runtime=runtime,
    )

    assert outcome.missing == ["run全体の不足"]


@pytest.mark.asyncio
async def test_review_drops_out_of_range_duplicate_and_over_cap_selections() -> None:
    """S1(選別結果の復元)。範囲外/重複/採用上限超過をRun単位で決定的にdropする。

    S2でcap値がRun単位の15になったため、16件目で上限超過が起きることを検証する。
    """
    tasks = [
        _collected_task(
            task_index=0,
            internal_hits=[
                _internal_hit(assessment_id=1000 + i, curation_id=i + 1, title=f"c{i}")
                for i in range(16)
            ],
        )
    ]
    selections = [
        {"candidate_index": index, "claim": f"claim-{index}", "why_selected": "w"}
        for index in [0, 0, *range(1, 16), 99]
    ]
    runtime = ScriptedAgentRuntime([_draft(selections)])

    outcome = await _review(tasks=tasks, reviewer_runtime=runtime)

    assert len(outcome.internal_evidence) == 15
    # dup(index 0)・上限超過(16件目のindex 15)・範囲外(index 99)の3件がdropされる。
    assert outcome.dropped_selection_count == 3


@pytest.mark.asyncio
async def test_review_retries_at_most_twice_with_the_same_typed_input() -> None:
    runtime = ScriptedAgentRuntime(
        [
            AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
            _draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}]),
        ]
    )

    outcome = await _review(
        tasks=[_collected_task(task_index=0, internal_hits=[_internal_hit()])],
        reviewer_runtime=runtime,
    )

    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert runtime.calls[0].input is runtime.calls[1].input
    assert outcome.failure_reason is None
    assert len(outcome.internal_evidence) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        pytest.param(
            AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON),
            "response_not_json",
            id="runtime-defect",
        ),
        pytest.param(
            AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT),
            "timeout",
            id="provider-reason",
        ),
        # policy.REVIEWER_ERROR_REASON: 未分類 provider error の安全な fallback。
        pytest.param(AIProviderError(), "reviewer_error", id="provider-fallback"),
        # policy.REVIEWER_TIMEOUT_REASON: asyncio.wait_for相当のtimeout分類。
        pytest.param(TimeoutError(), "reviewer_timeout", id="timeout"),
    ],
)
async def test_review_classifies_failure_reason_after_two_exhausted_attempts(
    failure: BaseException,
    expected_reason: str,
) -> None:
    """S1(精査の失敗)。2 attempt尽きるとRun全体が根拠ゼロで終わり例外を投げない。

    2 taskに候補があっても、reviewerの呼び出しはRunにつき1回(最大2 attempt)
    であり、taskごとに新しいattempt列は発生しない。
    """
    runtime = ScriptedAgentRuntime([failure, failure])
    tasks = [
        _collected_task(task_index=0, internal_hits=[_internal_hit()]),
        _collected_task(task_index=1, external_candidates=[_external_candidate()]),
    ]

    outcome = await _review(tasks=tasks, reviewer_runtime=runtime)

    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert outcome.internal_evidence == []
    assert outcome.external_evidence == []
    assert outcome.missing == []
    assert outcome.failure_reason == expected_reason


@pytest.mark.asyncio
async def test_review_retries_after_invalid_draft_and_drops_invalid_selections() -> (
    None
):
    """claimが空のselectionはfinalize_review_draft()でValidationErrorとなり

    attempt 1が失敗として扱われる(schema自体は妥当なのでruntimeは例外を
    投げない)。attempt 2で重複/範囲外がdropされつつ有効な選択だけが残る。
    """
    runtime = ScriptedAgentRuntime(
        [
            _draft([{"candidate_index": 0, "claim": "", "why_selected": "w"}]),
            _draft(
                [
                    {"candidate_index": 0, "claim": "first", "why_selected": "w"},
                    {"candidate_index": 0, "claim": "duplicate", "why_selected": "w"},
                    {"candidate_index": 99, "claim": "out", "why_selected": "w"},
                ]
            ),
        ]
    )

    outcome = await _review(
        tasks=[
            _collected_task(task_index=0, external_candidates=[_external_candidate()])
        ],
        reviewer_runtime=runtime,
    )

    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert outcome.failure_reason is None
    assert [item.claim for item in outcome.external_evidence] == ["first"]
    assert outcome.dropped_selection_count == 2


@pytest.mark.asyncio
async def test_review_propagates_unclassified_exception_without_retry() -> None:
    """分類対象外の例外はreview()が握りつぶさず、即座に呼び出し元へ伝播する。"""
    error = RuntimeError("unclassified reviewer error")
    runtime = ScriptedAgentRuntime([error])

    with pytest.raises(RuntimeError) as raised:
        await _review(
            tasks=[_collected_task(task_index=0, internal_hits=[_internal_hit()])],
            reviewer_runtime=runtime,
        )

    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]


class _NeverCompletingRuntime:
    """timeoutで打ち切られるまで応答しないruntime double。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.attempt_numbers: list[int] = []

    async def invoke(self, agent: object, input: Any, *, attempt_number: int) -> Any:
        del agent, input
        self.attempt_numbers.append(attempt_number)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _shorten_review_timeout(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    original_wait_for = asyncio.wait_for
    observed: list[float] = []

    async def wait_for(awaitable: Any, timeout: float) -> Any:
        observed.append(timeout)
        bounded_timeout = 0.001 if timeout == 30 else timeout
        return await original_wait_for(awaitable, timeout=bounded_timeout)

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    return observed


@pytest.mark.asyncio
async def test_review_timeout_backstop_cancels_the_runtime_and_retries_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio.wait_for(timeout=EVIDENCE_REVIEW_TIMEOUT_SECONDS)の実配線を検証する

    (TimeoutErrorを直接注入するだけの分類テストとは別に、実際にcancelされる
    ことを確かめる)。
    """
    observed_timeouts = _shorten_review_timeout(monkeypatch)
    runtime = _NeverCompletingRuntime()

    outcome = await asyncio.wait_for(
        _review(
            tasks=[_collected_task(task_index=0, internal_hits=[_internal_hit()])],
            reviewer_runtime=runtime,
        ),
        timeout=0.5,
    )

    assert runtime.cancelled is True
    assert runtime.attempt_numbers == [1, 2]
    assert outcome.failure_reason == "reviewer_timeout"
    assert observed_timeouts.count(30) == 2
