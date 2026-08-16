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

from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.evidence_review.answer_evidence import (
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.evidence_review.preparation import EvidenceReviewInput
from app.agent.evidence_review.reviewer import EvidenceReviewer
from app.agent.evidence_review.selection import EvidenceReviewDraft
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderError, AIProviderNetworkError
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from tests.agent.evidence_review._builders import (
    AS_OF,
    collected_task,
    external_candidate,
    internal_hit,
)
from tests.agent.runtime._fakes import ScriptedAgentRuntime

# 中身を見ないテスト用。空なのはどのtasksでも妥当なため。
_ANY_REVIEW_DRAFT = EvidenceReviewDraft.model_validate(
    {"selections": [], "missing": []}
)


def _review_draft(
    selections: list[dict[str, Any]],
    *,
    missing: list[str] | None = None,
) -> EvidenceReviewDraft:
    return EvidenceReviewDraft.model_validate(
        {"selections": selections, "missing": missing or []}
    )


async def _review(
    *,
    tasks: list[Any],
    as_of: datetime = AS_OF,
    reviewer_runtime: Any,
) -> EvidenceRunResult:
    reviewer = EvidenceReviewer()
    return await reviewer.review(
        tasks=tasks,
        as_of=as_of,
        reviewer_runtime=reviewer_runtime,
    )


@pytest.mark.asyncio
async def test_review_is_called_exactly_once_for_a_multi_task_run() -> None:
    """S1 A1。複数taskがあってもreviewer_runtime.invokeは1 attemptにつき1回。"""
    runtime = ScriptedAgentRuntime([_ANY_REVIEW_DRAFT])
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001,
                    curation_id=1,
                    title="internal title",
                    summary="s",
                )
            ],
        ),
        collected_task(
            task_index=1,
            external_candidates=[external_candidate("https://example.com/a")],
        ),
    ]

    await _review(tasks=tasks, reviewer_runtime=runtime)

    assert len(runtime.calls) == 1
    assert runtime.calls[0].agent is EVIDENCE_REVIEWER_AGENT


@pytest.mark.asyncio
async def test_review_passes_evidence_review_input_to_runtime() -> None:
    """runtimeへ渡す入力はEvidenceReviewInputである。"""
    runtime = ScriptedAgentRuntime([_ANY_REVIEW_DRAFT])

    await _review(
        tasks=[collected_task(task_index=0)],
        reviewer_runtime=runtime,
    )

    assert type(runtime.calls[0].input) is EvidenceReviewInput


@pytest.mark.asyncio
async def test_review_passes_as_of_to_the_reviewer_input_unchanged() -> None:
    """受け取ったas_ofは別の時刻に置き換えずReviewer入力へ渡す。"""
    runtime = ScriptedAgentRuntime([_ANY_REVIEW_DRAFT])
    as_of = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    await _review(
        tasks=[],
        as_of=as_of,
        reviewer_runtime=runtime,
    )

    assert runtime.calls[0].input.as_of == as_of


@pytest.mark.asyncio
async def test_selection_restores_candidate_and_task_from_a_cross_task_index() -> None:
    """S1(選別結果の復元)。グループをまたいだindexから候補と所属taskが復元される。

    復元規則の正本はtest_preparation.py。ここではreview()経由の実配線を確認する。
    """
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int-1", summary="s"
                ),
                internal_hit(
                    assessment_id=1002, curation_id=2, title="A-int-2", summary="s"
                ),
            ],
        ),
        collected_task(
            task_index=1,
            external_candidates=[
                external_candidate("https://example.com/b1", title="B-ext-1"),
                external_candidate("https://example.com/b2", title="B-ext-2"),
            ],
        ),
    ]
    runtime = ScriptedAgentRuntime(
        [
            _review_draft(
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

    result = await _review(tasks=tasks, reviewer_runtime=runtime)

    assert isinstance(result, EvidenceRunCompleted)
    assert [
        (item.title, item.task_index)
        for item in result.answer_evidence.internal_articles
    ] == [("A-int-2", 0)]
    assert [
        (item.title, item.task_index)
        for item in result.answer_evidence.external_sources
    ] == [("B-ext-2", 1)]
    assert result.answer_evidence.internal_articles[0].source_ref == "0-1"
    assert result.answer_evidence.external_sources[0].source_ref == "1-3"


@pytest.mark.asyncio
async def test_review_propagates_missing_as_a_single_run_level_value() -> None:
    """S1(何ができていないかの表明)。missingはRun全体で1本として返る。"""
    runtime = ScriptedAgentRuntime([_review_draft([], missing=["run全体の不足"])])

    result = await _review(
        tasks=[
            collected_task(
                task_index=0,
                internal_hits=[
                    internal_hit(
                        assessment_id=1001,
                        curation_id=1,
                        title="internal title",
                        summary="s",
                    )
                ],
            )
        ],
        reviewer_runtime=runtime,
    )

    assert isinstance(result, EvidenceRunCompleted)
    assert result.review_missing == ("run全体の不足",)


@pytest.mark.asyncio
async def test_review_retries_at_most_twice_with_the_same_typed_input() -> None:
    runtime = ScriptedAgentRuntime(
        [
            AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
            _review_draft(
                [{"candidate_index": 0, "claim": "claim", "why_selected": "w"}]
            ),
        ]
    )

    result = await _review(
        tasks=[
            collected_task(
                task_index=0,
                internal_hits=[
                    internal_hit(
                        assessment_id=1001,
                        curation_id=1,
                        title="internal title",
                        summary="s",
                    )
                ],
            )
        ],
        reviewer_runtime=runtime,
    )

    assert isinstance(result, EvidenceRunCompleted)
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert runtime.calls[0].input is runtime.calls[1].input
    assert len(result.answer_evidence.internal_articles) == 1


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
        pytest.param(
            AIProviderNetworkError(),
            "ai_error_network",
            id="provider-code",
        ),
        # asyncio.wait_for相当のtimeout分類。
        pytest.param(TimeoutError(), "reviewer_timeout", id="timeout"),
    ],
)
async def test_review_classifies_failure_reason_after_two_exhausted_attempts(
    failure: BaseException,
    expected_reason: str,
) -> None:
    """S1(精査の失敗)。2 attempt尽きるとRun全体が失敗結果で終わり例外を投げない。

    2 taskに候補があっても、reviewerの呼び出しはRunにつき1回(最大2 attempt)
    であり、taskごとに新しいattempt列は発生しない。
    """
    runtime = ScriptedAgentRuntime([failure, failure])
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001,
                    curation_id=1,
                    title="internal title",
                    summary="s",
                )
            ],
        ),
        collected_task(
            task_index=1,
            external_candidates=[external_candidate("https://example.com/a")],
        ),
    ]

    result = await _review(tasks=tasks, reviewer_runtime=runtime)

    assert isinstance(result, EvidenceRunFailed)
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert result.failure_reason == expected_reason


@pytest.mark.asyncio
async def test_review_propagates_unclassified_provider_error() -> None:
    """回復クラスを宣言しない裸のprovider errorは失敗理由に変換せず伝播する。"""
    runtime = ScriptedAgentRuntime([AIProviderError()])
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001,
                    curation_id=1,
                    title="internal title",
                    summary="s",
                )
            ],
        )
    ]

    with pytest.raises(AIProviderError):
        await _review(tasks=tasks, reviewer_runtime=runtime)

    assert [call.attempt_number for call in runtime.calls] == [1]


@pytest.mark.asyncio
async def test_review_retries_after_invalid_draft_and_drops_invalid_selections() -> (
    None
):
    """claimが空のselectionはfrom_draft()でValidationErrorとなり

    attempt 1が失敗として扱われる(schema自体は妥当なのでruntimeは例外を
    投げない)。attempt 2で重複/範囲外を除き、有効な選択だけが残る。
    """
    runtime = ScriptedAgentRuntime(
        [
            _review_draft([{"candidate_index": 0, "claim": "", "why_selected": "w"}]),
            _review_draft(
                [
                    {"candidate_index": 0, "claim": "first", "why_selected": "w"},
                    {"candidate_index": 0, "claim": "duplicate", "why_selected": "w"},
                    {"candidate_index": 99, "claim": "out", "why_selected": "w"},
                ]
            ),
        ]
    )

    result = await _review(
        tasks=[
            collected_task(
                task_index=0,
                external_candidates=[external_candidate("https://example.com/a")],
            )
        ],
        reviewer_runtime=runtime,
    )

    assert isinstance(result, EvidenceRunCompleted)
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert [item.claim for item in result.answer_evidence.external_sources] == ["first"]


@pytest.mark.asyncio
async def test_review_propagates_unclassified_exception_without_retry() -> None:
    """分類対象外の例外はreview()が握りつぶさず、即座に呼び出し元へ伝播する。"""
    error = RuntimeError("unclassified reviewer error")
    runtime = ScriptedAgentRuntime([error])

    with pytest.raises(RuntimeError) as raised:
        await _review(
            tasks=[
                collected_task(
                    task_index=0,
                    internal_hits=[
                        internal_hit(
                            assessment_id=1001,
                            curation_id=1,
                            title="internal title",
                            summary="s",
                        )
                    ],
                )
            ],
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
    """asyncio.wait_for(timeout=30)の実配線を検証する

    (TimeoutErrorを直接注入するだけの分類テストとは別に、実際にcancelされる
    ことを確かめる)。
    """
    observed_timeouts = _shorten_review_timeout(monkeypatch)
    runtime = _NeverCompletingRuntime()

    result = await asyncio.wait_for(
        _review(
            tasks=[
                collected_task(
                    task_index=0,
                    internal_hits=[
                        internal_hit(
                            assessment_id=1001,
                            curation_id=1,
                            title="internal title",
                            summary="s",
                        )
                    ],
                )
            ],
            reviewer_runtime=runtime,
        ),
        timeout=0.5,
    )

    assert isinstance(result, EvidenceRunFailed)
    assert runtime.cancelled is True
    assert runtime.attempt_numbers == [1, 2]
    assert result.failure_reason == "reviewer_timeout"
    assert observed_timeouts.count(30) == 2
