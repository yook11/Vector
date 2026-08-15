"""Reviewerが返す未確定選択の契約(draftの受理とResponseへの確定)。

LLMのdraftは自由記述欄を丸めてから`EvidenceReviewerResponse`へ確定し、
Run単位の選択数・missing数の上限を超えた入力は受け付けない。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.contract import EVIDENCE_REVIEWER_SELECTION_LIMIT
from app.agent.evidence_review.selection import (
    EvidenceReviewDraft,
    EvidenceReviewerResponse,
    EvidenceReviewerSelection,
    ReviewSelectionDraft,
)


def test_review_selection_draft_rejects_negative_index_before_finalization() -> None:
    with pytest.raises(ValidationError):
        ReviewSelectionDraft(candidate_index=-1, claim="claim", why_selected="why")

    draft = EvidenceReviewDraft.model_validate(
        {
            "selections": [
                {"candidate_index": 1, "claim": "claim", "why_selected": "why"}
            ],
            "missing": ["一次情報が不足"],
        }
    )
    assert draft.selections[0].candidate_index == 1


def test_reviewer_response_rejects_more_than_the_selection_limit() -> None:
    selections = [
        EvidenceReviewerSelection(
            candidate_index=index,
            claim=f"claim-{index}",
            why_selected="why",
        )
        for index in range(EVIDENCE_REVIEWER_SELECTION_LIMIT + 1)
    ]

    with pytest.raises(ValidationError):
        EvidenceReviewerResponse(selections=selections)


def test_from_draft_clamps_values_to_existing_contract() -> None:
    draft = EvidenceReviewDraft.model_validate(
        {
            "selections": [
                {
                    "candidate_index": 0,
                    "claim": "c" * 350,
                    "why_selected": "w" * 350,
                }
            ],
            "missing": ["m" * 250],
        }
    )

    result = EvidenceReviewerResponse.from_draft(draft)

    assert (
        len(result.selections[0].claim),
        len(result.selections[0].why_selected),
        len(result.missing[0]),
    ) == (300, 300, 200)


def test_from_draft_clamps_missing_item_count_to_the_run_wide_limit() -> None:
    """S2(選別結果の復元)。reviewerが9件以上のmissingを返しても

    Run単位のmissing上限(8)へclampされる。
    """
    draft = EvidenceReviewDraft.model_validate(
        {
            "selections": [],
            "missing": [f"missing-{index}" for index in range(9)],
        }
    )

    result = EvidenceReviewerResponse.from_draft(draft)

    assert len(result.missing) == 8
    assert result.missing == tuple(f"missing-{index}" for index in range(8))
