"""EvidenceCollectionOutcome の DTO 不変条件テスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory


def _external_outcome() -> ExternalSearchOutcome:
    return ExternalSearchOutcome(
        tasks=[],
        evidence=[],
        task_reports=[],
        effective_agent_count=0,
    )


def _hit() -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=1,
        title="NVIDIA",
        summary="NVIDIA summary",
        assessment_result=InScope(
            category=InScopeCategory.AI,
            investor_take="投資家視点",
            key_points=[],
        ),
    )
    return InternalArticleSearchHit(
        assessment_id=1001,
        article=article,
        content=InternalArticleContent.from_article(article, published_at=None),
        distance=0.1,
    )


def test_outcome_rejects_external_search_and_external_failure() -> None:
    with pytest.raises(ValidationError):
        EvidenceCollectionOutcome(
            external_search=_external_outcome(),
            collection_failures=["external_search"],
        )


def test_outcome_allows_external_failure_when_search_is_absent() -> None:
    outcome = EvidenceCollectionOutcome(collection_failures=["external_search"])

    assert (outcome.external_search, outcome.collection_failures) == (
        None,
        ["external_search"],
    )


def test_outcome_rejects_duplicate_or_out_of_order_failures() -> None:
    for failures in (
        ["internal_search", "internal_search"],
        ["external_search", "external_search"],
        ["external_search", "internal_search"],
    ):
        with pytest.raises(ValidationError):
            EvidenceCollectionOutcome(collection_failures=failures)


def test_outcome_rejects_internal_hits_and_internal_failure() -> None:
    with pytest.raises(ValidationError):
        EvidenceCollectionOutcome(
            internal_hits=[_hit()],
            collection_failures=["internal_search"],
        )


def test_outcome_allows_zero_internal_hits_without_failure() -> None:
    outcome = EvidenceCollectionOutcome(internal_hits=[])

    assert outcome.collection_failures == []
