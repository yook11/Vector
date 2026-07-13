"""Evidence answer draft finalization tests."""

import pytest
from pydantic import ValidationError

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraftInvalidError,
    RawEvidenceAnswerDraft,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.validation import (
    finalize_evidence_answer_draft,
)
from app.agent.contract import ExternalUrlSource


def _evidence(source_ref: str = "1") -> AnswerEvidenceItem:
    return AnswerEvidenceItem(
        source=ExternalUrlSource(
            source_ref=source_ref,
            url="https://example.com/source",
            title="source",
            evidence_claim="確認済みの主張",
        ),
        text="確認済みの根拠本文",
    )


def test_finalizes_valid_answered_draft_without_defects() -> None:
    draft, defects = finalize_evidence_answer_draft(
        RawEvidenceAnswerDraft(
            sufficiency="answered",
            answer="根拠から確認できます。[[1]]",
            cited_refs=["1"],
            unfulfilled_requirement_ids=[],
        ),
        evidence=[_evidence()],
        requirement_ids=[],
    )

    assert draft.cited_refs == ["1"]
    assert defects == []


def test_rejects_unknown_sufficiency() -> None:
    with pytest.raises(EvidenceAnswerDraftInvalidError, match="unknown"):
        finalize_evidence_answer_draft(
            RawEvidenceAnswerDraft(
                sufficiency="partial",
                answer="部分回答です。[[1]]",
                cited_refs=["1"],
                unfulfilled_requirement_ids=[],
            ),
            evidence=[_evidence()],
            requirement_ids=[],
        )


def test_rejects_answered_draft_without_marker() -> None:
    with pytest.raises(EvidenceAnswerDraftInvalidError, match="citation marker"):
        finalize_evidence_answer_draft(
            RawEvidenceAnswerDraft(
                sufficiency="answered",
                answer="引用がありません。",
                cited_refs=["1"],
                unfulfilled_requirement_ids=[],
            ),
            evidence=[_evidence()],
            requirement_ids=[],
        )


def test_rejects_marker_missing_from_evidence() -> None:
    with pytest.raises(EvidenceAnswerDraftInvalidError, match=r"\[\[2\]\]"):
        finalize_evidence_answer_draft(
            RawEvidenceAnswerDraft(
                sufficiency="answered",
                answer="不実在の引用です。[[2]]",
                cited_refs=["2"],
                unfulfilled_requirement_ids=[],
            ),
            evidence=[_evidence("1")],
            requirement_ids=[],
        )


def test_empty_evidence_accepts_valid_insufficient_draft() -> None:
    draft, defects = finalize_evidence_answer_draft(
        RawEvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="引用できる根拠がありません。",
            cited_refs=[],
            missing_aspects=["引用できる根拠"],
            unfulfilled_requirement_ids=[],
        ),
        evidence=[],
        requirement_ids=[],
    )

    assert draft.sufficiency == "insufficient"
    assert draft.cited_refs == []
    assert defects == []


def test_reports_every_deterministic_cleanup_defect() -> None:
    draft, defects = finalize_evidence_answer_draft(
        RawEvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="一部だけ確認できます。[[1]]",
            cited_refs=["1", "", "1", 2],
            missing_aspects=["", "一次情報", "一次情報", False],
            unfulfilled_requirement_ids=[],
        ),
        evidence=[_evidence()],
        requirement_ids=[],
    )

    assert draft.cited_refs == ["1"]
    assert draft.missing_aspects == ["一次情報"]
    assert set(defects) == {
        "blank_cited_refs_removed",
        "duplicate_cited_refs_removed",
        "non_string_cited_refs_removed",
        "blank_missing_aspects_removed",
        "duplicate_missing_aspects_removed",
        "non_string_missing_aspects_removed",
    }


def test_orders_valid_unfulfilled_ids_by_canonical_requirement_order() -> None:
    requirement_ids = ["c1", "c2", "p1", "p2"]
    draft, defects = finalize_evidence_answer_draft(
        RawEvidenceAnswerDraft(
            sufficiency="answered",
            answer="根拠から確認できます。[[1]]",
            cited_refs=["1"],
            unfulfilled_requirement_ids=["p2", "c2", "p1", "c1"],
        ),
        evidence=[_evidence()],
        requirement_ids=requirement_ids,
    )

    assert (draft.unfulfilled_requirement_ids, defects) == (requirement_ids, [])


def test_cleans_invalid_unfulfilled_ids_and_reports_each_defect_once() -> None:
    draft, defects = finalize_evidence_answer_draft(
        RawEvidenceAnswerDraft(
            sufficiency="answered",
            answer="根拠から確認できます。[[1]]",
            cited_refs=["1"],
            unfulfilled_requirement_ids=[
                1,
                None,
                " ",
                "\n",
                "p1",
                "p1",
                "unknown",
                "unknown",
                " c1 ",
            ],
        ),
        evidence=[_evidence()],
        requirement_ids=["c1", "p1"],
    )

    assert (draft.unfulfilled_requirement_ids, sorted(defects)) == (
        ["c1", "p1"],
        sorted(
            [
                "blank_unfulfilled_requirement_ids_removed",
                "duplicate_unfulfilled_requirement_ids_removed",
                "non_string_unfulfilled_requirement_ids_removed",
                "unknown_unfulfilled_requirement_ids_removed",
            ]
        ),
    )


def test_completes_missing_unfulfilled_ids_and_reports_defect() -> None:
    raw = RawEvidenceAnswerDraft(
        sufficiency="answered",
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
    )

    draft, defects = finalize_evidence_answer_draft(
        raw,
        evidence=[_evidence()],
        requirement_ids=["c1"],
    )

    assert (
        "unfulfilled_requirement_ids" not in raw.model_fields_set,
        draft.unfulfilled_requirement_ids,
        defects,
    ) == (True, [], ["unfulfilled_requirement_ids_completed"])


def test_explicit_empty_unfulfilled_ids_do_not_report_completion_defect() -> None:
    raw = RawEvidenceAnswerDraft(
        sufficiency="answered",
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
        unfulfilled_requirement_ids=[],
    )

    draft, defects = finalize_evidence_answer_draft(
        raw,
        evidence=[_evidence()],
        requirement_ids=["c1"],
    )

    assert (
        "unfulfilled_requirement_ids" in raw.model_fields_set,
        draft.unfulfilled_requirement_ids,
        defects,
    ) == (True, [], [])


def test_empty_requirement_allowlist_removes_all_reported_ids_as_unknown() -> None:
    draft, defects = finalize_evidence_answer_draft(
        RawEvidenceAnswerDraft(
            sufficiency="answered",
            answer="根拠から確認できます。[[1]]",
            cited_refs=["1"],
            unfulfilled_requirement_ids=["c1", "p1"],
        ),
        evidence=[_evidence()],
        requirement_ids=[],
    )

    assert (draft.unfulfilled_requirement_ids, defects) == (
        [],
        ["unknown_unfulfilled_requirement_ids_removed"],
    )


@pytest.mark.parametrize("answer", [None, 1, "  "])
def test_rejects_answer_that_cannot_form_strict_draft(answer: object) -> None:
    with pytest.raises(ValidationError):
        finalize_evidence_answer_draft(
            RawEvidenceAnswerDraft(
                sufficiency="insufficient",
                answer=answer,
                missing_aspects=["回答本文"],
                unfulfilled_requirement_ids=[],
            ),
            evidence=[],
            requirement_ids=[],
        )
