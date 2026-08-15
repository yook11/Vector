"""ReviewedEvidenceがAnswerEvidenceとRun診断を対応づける契約テスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection import ResearchTaskReport
from app.agent.evidence_collection.external_search import ExternalSearchEvidence
from app.agent.evidence_review import (
    AnswerEvidence,
    EvidenceReviewReport,
    ReviewedEvidence,
)
from app.agent.evidence_review.result import InternalArticleEvidence


def _report(*, task_index: int = 0) -> ResearchTaskReport:
    return ResearchTaskReport(
        task_index=task_index,
        research_goal=f"goal-{task_index}",
        internal_collection="succeeded",
        external_collection="succeeded",
    )


def _review_report(
    *,
    internal_evidence_count: int = 0,
    external_evidence_count: int = 0,
) -> EvidenceReviewReport:
    return EvidenceReviewReport(
        review="succeeded",
        internal_evidence_count=internal_evidence_count,
        external_evidence_count=external_evidence_count,
    )


def _internal_evidence(
    *,
    curation_id: int = 1,
    task_index: int = 0,
    source_ref: str = "0-0",
) -> InternalArticleEvidence:
    return InternalArticleEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        assessment_id=1000 + curation_id,
        curation_id=curation_id,
        title="internal",
        summary="summary",
    )


def _external_evidence(
    *,
    task_index: int = 0,
    source_ref: str = "0-1",
) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        url=f"https://example.com/{source_ref}",
        title="external",
    )


def _outcome(
    *,
    task_reports: list[ResearchTaskReport],
    answer_evidence: AnswerEvidence | None = None,
    review: EvidenceReviewReport | None = None,
) -> ReviewedEvidence:
    return ReviewedEvidence(
        answer_evidence=answer_evidence or AnswerEvidence(),
        task_reports=task_reports,
        review=review or _review_report(),
    )


@pytest.mark.parametrize(
    "legacy_field,value",
    [
        pytest.param("collection_failures", [], id="collection-failures"),
        pytest.param("internal_evidence", [], id="internal-evidence"),
        pytest.param("external_search", None, id="external-search"),
        pytest.param("internal_deduplicated_count", 0, id="deduplicated-count"),
    ],
)
def test_outcome_rejects_legacy_evidence_fields(
    legacy_field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        ReviewedEvidence.model_validate(
            {
                "answer_evidence": AnswerEvidence(),
                "task_reports": [_report()],
                "review": _review_report(),
                legacy_field: value,
            }
        )


@pytest.mark.parametrize(
    "task_indexes",
    [
        pytest.param([0, 0], id="duplicate"),
        pytest.param([0, 2], id="missing"),
    ],
)
def test_outcome_requires_task_reports_covering_0_to_n_minus_1(
    task_indexes: list[int],
) -> None:
    with pytest.raises(ValidationError):
        _outcome(task_reports=[_report(task_index=index) for index in task_indexes])


def test_outcome_accepts_matching_answer_evidence_counts() -> None:
    answer_evidence = AnswerEvidence(
        internal_articles=(_internal_evidence(),),
        external_sources=(_external_evidence(),),
    )

    outcome = _outcome(
        task_reports=[_report()],
        answer_evidence=answer_evidence,
        review=_review_report(
            internal_evidence_count=1,
            external_evidence_count=1,
        ),
    )

    assert outcome.answer_evidence is answer_evidence


@pytest.mark.parametrize(
    "review",
    [
        pytest.param(
            _review_report(internal_evidence_count=2),
            id="internal-count",
        ),
        pytest.param(
            _review_report(external_evidence_count=2),
            id="external-count",
        ),
    ],
)
def test_outcome_rejects_review_counts_that_do_not_match_answer_evidence(
    review: EvidenceReviewReport,
) -> None:
    answer_evidence = AnswerEvidence(
        internal_articles=(_internal_evidence(),),
        external_sources=(_external_evidence(),),
    )

    with pytest.raises(ValidationError):
        _outcome(
            task_reports=[_report()],
            answer_evidence=answer_evidence,
            review=review,
        )


def test_outcome_rejects_evidence_for_unreported_task() -> None:
    answer_evidence = AnswerEvidence(
        internal_articles=(_internal_evidence(task_index=1),),
    )

    with pytest.raises(ValidationError):
        _outcome(
            task_reports=[_report(task_index=0)],
            answer_evidence=answer_evidence,
            review=_review_report(internal_evidence_count=1),
        )


def test_outcome_rejects_empty_task_reports() -> None:
    with pytest.raises(ValidationError):
        _outcome(task_reports=[])
