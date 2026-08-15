"""review_collected_news() がEvidence Runの確定結果へ変換する契約テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    ResearchTaskReport,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_review import (
    EvidenceReviewer,
    EvidenceRunCompleted,
    EvidenceRunFailed,
)
from app.agent.evidence_review.draft import EvidenceReviewDraft
from app.agent.evidence_review.run_review import review_collected_news
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.runtime._fakes import ScriptedAgentRuntime

_AS_OF = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


class _UnusedExternalSearchTool:
    @property
    def name(self) -> str:
        return "external_search"

    async def invoke(self, input: object) -> list[object]:
        raise AssertionError(f"external search must not be called: {input!r}")


def _internal_hit() -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=1,
        title="internal candidate",
        summary="summary",
        assessment_result=InScope(
            category=InScopeCategory.AI,
            investor_take="investor take",
            key_points=[],
        ),
    )
    return InternalArticleSearchHit(
        assessment_id=1001,
        article=article,
        content=InternalArticleContent.from_article(article, published_at=None),
        distance=0.1,
    )


def _collected_news(*, with_candidate: bool) -> CollectedNews:
    internal_hits = [_internal_hit()] if with_candidate else []
    return CollectedNews(
        tasks=[
            CollectedTask(
                task_index=0,
                research_goal="NVIDIAの直近動向を確認する",
                internal_hits=internal_hits,
                external_candidates=[],
                executed_queries=(),
                report=ResearchTaskReport(
                    task_index=0,
                    research_goal="NVIDIAの直近動向を確認する",
                    internal_collection="succeeded",
                    external_collection="succeeded",
                    internal_candidate_count=len(internal_hits),
                ),
            )
        ],
        requested_agent_count=1,
        effective_agent_count=1,
    )


def _external(reviewer_runtime: ScriptedAgentRuntime) -> ExternalResearchRuntime:
    return ExternalResearchRuntime(
        query_runtime=ScriptedAgentRuntime([]),
        reviewer_runtime=reviewer_runtime,
        search_tool=_UnusedExternalSearchTool(),  # type: ignore[arg-type]
    )


def _draft(
    selections: list[dict[str, object]] | None = None,
    *,
    missing: list[str] | None = None,
) -> EvidenceReviewDraft:
    return EvidenceReviewDraft.model_validate(
        {"selections": selections or [], "missing": missing or []}
    )


async def _review(
    *,
    with_candidate: bool,
    reviewer_runtime: ScriptedAgentRuntime,
) -> EvidenceRunCompleted | EvidenceRunFailed:
    return await review_collected_news(
        collected_news=_collected_news(with_candidate=with_candidate),
        reviewer=EvidenceReviewer(),
        external=_external(reviewer_runtime),
        as_of=_AS_OF,
    )


@pytest.mark.asyncio
async def test_empty_candidates_complete_without_starting_reviewer() -> None:
    reviewer_runtime = ScriptedAgentRuntime([])

    result = await _review(
        with_candidate=False,
        reviewer_runtime=reviewer_runtime,
    )

    assert isinstance(result, EvidenceRunCompleted)
    assert (
        result.answer_evidence.count,
        result.review_missing,
        reviewer_runtime.calls,
    ) == (
        0,
        (),
        [],
    )


@pytest.mark.asyncio
async def test_successful_review_with_no_adopted_evidence_completes() -> None:
    reviewer_runtime = ScriptedAgentRuntime(
        [_draft([], missing=["公式発表を確認できませんでした"])]
    )

    result = await _review(
        with_candidate=True,
        reviewer_runtime=reviewer_runtime,
    )

    assert isinstance(result, EvidenceRunCompleted)
    assert (result.answer_evidence.count, result.review_missing) == (
        0,
        ("公式発表を確認できませんでした",),
    )


@pytest.mark.asyncio
async def test_successful_review_completes_with_restored_evidence() -> None:
    reviewer_runtime = ScriptedAgentRuntime(
        [_draft([{"candidate_index": 0, "claim": "claim", "why_selected": "why"}])]
    )

    result = await _review(
        with_candidate=True,
        reviewer_runtime=reviewer_runtime,
    )

    assert isinstance(result, EvidenceRunCompleted)
    assert [item.title for item in result.answer_evidence.internal_articles] == [
        "internal candidate"
    ]


@pytest.mark.asyncio
async def test_reviewer_failure_becomes_failed_result_with_safe_reason() -> None:
    failure = AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)
    reviewer_runtime = ScriptedAgentRuntime([failure, failure])

    result = await _review(
        with_candidate=True,
        reviewer_runtime=reviewer_runtime,
    )

    assert isinstance(result, EvidenceRunFailed)
    assert result.failure_reason == "response_not_json"
