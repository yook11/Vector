"""EvidenceReviewer.review() の単体契約テスト(D4-S1)。

現行 AnsweringRunner._select_external_evidence と同じ attempt / timeout /
失敗分類規則を、統合candidate列を受け取る新契約として検証する。
production 未実装のため getattr ガードで参照する。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any

import pytest

from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.planning.contract import ResearchTask
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderError, AIProviderNetworkError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from tests.agent.runtime._fakes import ScriptedAgentRuntime

_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _required_module(module_name: str) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"D4-S1 evidence_review module is missing: {module_name} ({exc.name})"
        )


def _required_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(
            f"D4-S1 evidence_review contract is missing: {module.__name__}.{name}"
        )
    return getattr(module, name)


def _contracts() -> ModuleType:
    return _required_module("app.agent.evidence_collection.evidence_review.contract")


def _reviewer_module() -> ModuleType:
    return _required_module("app.agent.evidence_collection.evidence_review.reviewer")


def _reviewer() -> Any:
    reviewer_type = _required_attribute(_reviewer_module(), "EvidenceReviewer")
    return reviewer_type()


def _task(goal: str = "NVIDIA の最新動向を確認する") -> ResearchTask:
    return ResearchTask(research_goal=goal, article_search_queries=["query"])


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
) -> Any:
    draft_type = _required_attribute(_contracts(), "EvidenceReviewDraft")
    return draft_type.model_validate(
        {"selections": selections or [], "missing": missing or []}
    )


async def _review(
    *,
    task_index: int = 0,
    task: ResearchTask | None = None,
    content_requirements: tuple[str, ...] = (),
    internal_hits: list[InternalArticleSearchHit] | None = None,
    external_candidates: list[ExternalSearchCandidate] | None = None,
    as_of: datetime = _AS_OF,
    reviewer_runtime: Any,
) -> Any:
    reviewer = _reviewer()
    return await reviewer.review(
        task_index=task_index,
        task=task or _task(),
        content_requirements=content_requirements,
        internal_hits=internal_hits or [],
        external_candidates=external_candidates or [],
        as_of=as_of,
        reviewer_runtime=reviewer_runtime,
    )


@pytest.mark.asyncio
async def test_successful_review_returns_claimed_evidence_and_no_failure_reason() -> (
    None
):
    """保証するテスト条件 6。選別された内部根拠がclaimを持つ。"""
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                [{"candidate_index": 0, "claim": "internal claim", "why_selected": "w"}]
            )
        ]
    )

    outcome = await _review(
        internal_hits=[_internal_hit()],
        reviewer_runtime=runtime,
    )

    assert outcome.failure_reason is None
    assert len(outcome.internal_evidence) == 1
    assert outcome.internal_evidence[0].claim == "internal claim"
    assert outcome.external_evidence == []


@pytest.mark.asyncio
async def test_review_completes_with_only_internal_candidates() -> None:
    """保証するテスト条件 7(内部のみ)。外部候補ゼロでも精査が完了する。"""
    runtime = ScriptedAgentRuntime(
        [_draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}])]
    )

    outcome = await _review(
        internal_hits=[_internal_hit()],
        external_candidates=[],
        reviewer_runtime=runtime,
    )

    assert outcome.failure_reason is None
    assert len(outcome.internal_evidence) == 1


@pytest.mark.asyncio
async def test_review_completes_with_only_external_candidates() -> None:
    """保証するテスト条件 7(外部のみ)。内部候補ゼロでも精査が完了する。"""
    runtime = ScriptedAgentRuntime(
        [_draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}])]
    )

    outcome = await _review(
        internal_hits=[],
        external_candidates=[_external_candidate()],
        reviewer_runtime=runtime,
    )

    assert outcome.failure_reason is None
    assert len(outcome.external_evidence) == 1
    assert outcome.internal_evidence == []


@pytest.mark.asyncio
async def test_review_completes_with_empty_content_requirements() -> None:
    """content_requirementsが空でもresearch_goalだけで精査が完了する。"""
    runtime = ScriptedAgentRuntime([_draft([])])

    outcome = await _review(
        internal_hits=[_internal_hit()],
        content_requirements=(),
        reviewer_runtime=runtime,
    )

    assert outcome.failure_reason is None


@pytest.mark.asyncio
async def test_review_propagates_missing_from_draft() -> None:
    runtime = ScriptedAgentRuntime([_draft([], missing=["公式発表が見つからない"])])

    outcome = await _review(internal_hits=[_internal_hit()], reviewer_runtime=runtime)

    assert outcome.missing == ["公式発表が見つからない"]


@pytest.mark.asyncio
async def test_review_retries_at_most_twice_with_the_same_typed_input() -> None:
    runtime = ScriptedAgentRuntime(
        [
            AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
            _draft([{"candidate_index": 0, "claim": "claim", "why_selected": "w"}]),
        ]
    )

    outcome = await _review(
        internal_hits=[_internal_hit()],
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
    """保証するテスト条件 5。2 attempt尽きたtaskが根拠ゼロで終わり例外を投げない。"""
    runtime = ScriptedAgentRuntime([failure, failure])

    outcome = await _review(
        internal_hits=[_internal_hit()],
        external_candidates=[_external_candidate()],
        reviewer_runtime=runtime,
    )

    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert outcome.internal_evidence == []
    assert outcome.external_evidence == []
    assert outcome.missing == []
    assert outcome.failure_reason == expected_reason


@pytest.mark.asyncio
async def test_review_retries_after_invalid_draft_and_drops_invalid_selections() -> (
    None
):
    """D4-S1-T2対応表: 旧
    test_invalid_selector_draft_retries_without_invalid_evidence の移設先。

    claimが空のselectionはfinalize_review_draft()でValidationErrorとなり
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
        external_candidates=[_external_candidate()],
        reviewer_runtime=runtime,
    )

    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert outcome.failure_reason is None
    assert [item.claim for item in outcome.external_evidence] == ["first"]
    assert outcome.dropped_selection_count == 2


@pytest.mark.asyncio
async def test_review_propagates_unclassified_exception_without_retry() -> None:
    """D4-S1-T2対応表: 旧
    test_selector_unclassified_exception_does_not_retry_or_become_report の移設先。

    分類対象外の例外はreview()が握りつぶさず、即座に呼び出し元へ伝播する。
    """
    error = RuntimeError("unclassified reviewer error")
    runtime = ScriptedAgentRuntime([error])

    with pytest.raises(RuntimeError) as raised:
        await _review(internal_hits=[_internal_hit()], reviewer_runtime=runtime)

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
    """D4-S1-T2対応表: 旧
    test_selector_timeout_backstop_retries_twice_with_timeout_reason の移設先。

    asyncio.wait_for(timeout=EVIDENCE_REVIEW_TIMEOUT_SECONDS)の実配線を検証する
    (TimeoutErrorを直接注入するだけの分類テストとは別に、実際にcancelされる
    ことを確かめる)。
    """
    observed_timeouts = _shorten_review_timeout(monkeypatch)
    runtime = _NeverCompletingRuntime()

    outcome = await asyncio.wait_for(
        _review(internal_hits=[_internal_hit()], reviewer_runtime=runtime),
        timeout=0.5,
    )

    assert runtime.cancelled is True
    assert runtime.attempt_numbers == [1, 2]
    assert outcome.failure_reason == "reviewer_timeout"
    assert observed_timeouts.count(30) == 2


@pytest.mark.asyncio
async def test_review_drops_invalid_selections_before_returning_evidence() -> None:
    """保証するテスト条件 4。範囲外/重複indexをdropしdropped_selection_countへ計上。"""
    runtime = ScriptedAgentRuntime(
        [
            _draft(
                [
                    {"candidate_index": 0, "claim": "first", "why_selected": "w"},
                    {"candidate_index": 0, "claim": "duplicate", "why_selected": "w"},
                    {
                        "candidate_index": 99,
                        "claim": "out of range",
                        "why_selected": "w",
                    },
                ]
            )
        ]
    )

    outcome = await _review(
        internal_hits=[_internal_hit()],
        reviewer_runtime=runtime,
    )

    assert len(outcome.internal_evidence) == 1
    assert outcome.dropped_selection_count == 2
