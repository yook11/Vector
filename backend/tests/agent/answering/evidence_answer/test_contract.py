"""Evidence answer contract tests."""

import inspect
from dataclasses import FrozenInstanceError, fields
from importlib import import_module
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerer,
    RawEvidenceAnswerDraft,
)
from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow


def _first_input_annotation(method: object) -> object | None:
    parameter_names = tuple(inspect.signature(method).parameters)
    return get_type_hints(method).get(parameter_names[1])


def test_evidence_answer_boundaries_accept_request_without_previous_answer() -> None:
    assert (
        tuple(inspect.signature(EvidenceAnswerer.answer).parameters),
        tuple(inspect.signature(EvidenceAnswerFlow.answer).parameters),
        _first_input_annotation(EvidenceAnswerer.answer),
        _first_input_annotation(EvidenceAnswerFlow.answer),
    ) == (
        ("self", "request", "evidence", "target_time_window"),
        ("self", "request", "evidence", "target_time_window"),
        AnsweringRequest,
        AnsweringRequest,
    )


def test_evidence_answer_input_is_frozen_and_keeps_attempt_state_together() -> None:
    contract = import_module("app.agent.answering.evidence_answer.contract")
    input_type = getattr(contract, "EvidenceAnswerInput", None)

    assert input_type is not None, "EvidenceAnswerInput が未実装です"
    assert [field.name for field in fields(input_type)] == [
        "request",
        "evidence",
        "target_time_window",
        "previous_error",
    ]
    input = input_type(
        request=object(),
        evidence=(),
        target_time_window=None,
    )
    assert input.previous_error is None
    with pytest.raises(FrozenInstanceError):
        input.target_time_window = "changed"


def test_raw_draft_accepts_lenient_provider_values() -> None:
    draft = RawEvidenceAnswerDraft(
        sufficiency=1,
        answer=None,
        cited_refs=["1", 2, None],
        missing_aspects=["一次情報", False],
        unfulfilled_requirement_ids=["c1", 2, None],
    )

    assert draft.sufficiency == 1
    assert draft.answer is None
    assert draft.cited_refs == ["1", 2, None]
    assert draft.missing_aspects == ["一次情報", False]
    assert draft.unfulfilled_requirement_ids == ["c1", 2, None]


def test_raw_draft_rejects_non_array_unfulfilled_requirement_ids() -> None:
    with pytest.raises(ValidationError):
        RawEvidenceAnswerDraft.model_validate({"unfulfilled_requirement_ids": "c1"})


@pytest.mark.parametrize(
    "requirement_ids",
    [[], ["c1", "p1"]],
    ids=["empty", "non-empty"],
)
def test_strict_draft_preserves_unfulfilled_requirement_ids(
    requirement_ids: list[str],
) -> None:
    draft = EvidenceAnswerDraft(
        sufficiency="answered",
        answer="回答です。[[1]]",
        cited_refs=["1"],
        unfulfilled_requirement_ids=requirement_ids,
    )

    assert draft.unfulfilled_requirement_ids == requirement_ids


def test_strict_draft_rejects_non_string_unfulfilled_requirement_id() -> None:
    with pytest.raises(ValidationError):
        EvidenceAnswerDraft.model_validate(
            {
                "sufficiency": "answered",
                "answer": "回答です。[[1]]",
                "cited_refs": ["1"],
                "unfulfilled_requirement_ids": [1],
            }
        )


def test_evidence_answerer_documents_unfulfilled_ids_subset_contract() -> None:
    doc = EvidenceAnswerer.__doc__ or ""

    assert "unfulfilled_requirement_ids" in doc and (
        "部分集合" in doc or "subset" in doc.lower()
    )


@pytest.mark.parametrize(
    "draft",
    [
        {
            "sufficiency": "answered",
            "answer": "回答です。[[1]]",
            "cited_refs": [],
            "unfulfilled_requirement_ids": [],
        },
        {
            "sufficiency": "answered",
            "answer": "回答です。[[1]]",
            "cited_refs": ["1"],
            "missing_aspects": ["不足"],
            "unfulfilled_requirement_ids": ["c1"],
        },
        {
            "sufficiency": "insufficient",
            "answer": "不足しています。",
            "missing_aspects": [],
            "unfulfilled_requirement_ids": [],
        },
    ],
)
def test_strict_draft_rejects_sufficiency_contract_violations(
    draft: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceAnswerDraft.model_validate(draft)


@pytest.mark.parametrize("answer", ["", "   ", "\n"])
def test_strict_draft_rejects_blank_answer(answer: str) -> None:
    with pytest.raises(ValidationError):
        EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer=answer,
            missing_aspects=["不足"],
        )


@pytest.mark.parametrize("missing", ["", "   ", "\n"])
def test_strict_draft_rejects_blank_missing_aspect(missing: str) -> None:
    with pytest.raises(ValidationError):
        EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="断定できません。",
            missing_aspects=[missing],
        )
