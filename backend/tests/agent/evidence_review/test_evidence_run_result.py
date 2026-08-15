"""Evidence Runの確定結果型が表す成功・失敗の契約テスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.contract import EVIDENCE_REVIEW_MISSING_LIMIT
from app.agent.evidence_collection.external_search.contract import (
    MISSING_ITEM_MAX_CHARS,
)
from app.agent.evidence_review import (
    AnswerEvidence,
    EvidenceRunCompleted,
    EvidenceRunFailed,
    ExternalSearchEvidence,
)
from app.agent.evidence_review.result import InternalArticleEvidence


def _internal_evidence() -> InternalArticleEvidence:
    return InternalArticleEvidence(
        source_ref="0-0",
        task_index=0,
        claim="claim",
        why_selected="why",
        assessment_id=1001,
        curation_id=1,
        title="internal",
        summary="summary",
    )


def _external_evidence() -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref="0-1",
        task_index=0,
        claim="claim",
        why_selected="why",
        url="https://example.com/evidence",
        title="external",
    )


def test_completed_accepts_evidence_and_reviewer_missing() -> None:
    answer_evidence = AnswerEvidence(
        internal_articles=(_internal_evidence(),),
        external_sources=(_external_evidence(),),
    )

    result = EvidenceRunCompleted(
        answer_evidence=answer_evidence,
        review_missing=("公式発表を確認できませんでした",),
    )

    assert (result.answer_evidence, result.review_missing) == (
        answer_evidence,
        ("公式発表を確認できませんでした",),
    )


def test_completed_accepts_empty_evidence_and_empty_reviewer_missing() -> None:
    result = EvidenceRunCompleted(
        answer_evidence=AnswerEvidence(),
        review_missing=(),
    )

    assert (result.answer_evidence.count, result.review_missing) == (0, ())


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        pytest.param(
            EvidenceRunCompleted(
                answer_evidence=AnswerEvidence(),
                review_missing=(),
            ),
            "review_missing",
            ("changed",),
            id="completed",
        ),
        pytest.param(
            EvidenceRunFailed(failure_reason="reviewer_timeout"),
            "failure_reason",
            "changed",
            id="failed",
        ),
    ],
)
def test_run_result_variants_are_frozen(
    model: EvidenceRunCompleted | EvidenceRunFailed,
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        setattr(model, field, value)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        pytest.param(
            EvidenceRunCompleted,
            {
                "answer_evidence": AnswerEvidence(),
                "review_missing": (),
                "unexpected": "value",
            },
            id="completed",
        ),
        pytest.param(
            EvidenceRunFailed,
            {"failure_reason": "reviewer_timeout", "unexpected": "value"},
            id="failed",
        ),
    ],
)
def test_run_result_variants_reject_unknown_fields(
    model: type[EvidenceRunCompleted] | type[EvidenceRunFailed],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="missing"),
        pytest.param({"failure_reason": ""}, id="empty"),
    ],
)
def test_failed_requires_a_non_empty_failure_reason(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceRunFailed.model_validate(payload)


def test_failed_rejects_answer_evidence() -> None:
    with pytest.raises(ValidationError):
        EvidenceRunFailed.model_validate(
            {
                "failure_reason": "reviewer_timeout",
                "answer_evidence": AnswerEvidence(),
            }
        )


@pytest.mark.parametrize(
    "review_missing",
    [
        pytest.param(
            tuple(
                f"missing-{index}" for index in range(EVIDENCE_REVIEW_MISSING_LIMIT + 1)
            ),
            id="too-many-items",
        ),
        pytest.param(
            ("m" * (MISSING_ITEM_MAX_CHARS + 1),),
            id="item-too-long",
        ),
    ],
)
def test_completed_rejects_review_missing_over_reviewer_contract_caps(
    review_missing: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceRunCompleted(
            answer_evidence=AnswerEvidence(),
            review_missing=review_missing,
        )
