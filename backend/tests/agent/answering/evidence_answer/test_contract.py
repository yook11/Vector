"""Evidence answer contract tests."""

import inspect
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftGenerator,
    EvidenceAnswerer,
    RawEvidenceAnswerDraft,
)
from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow


def _first_input_annotation(method: object) -> object | None:
    parameter_names = tuple(inspect.signature(method).parameters)
    return get_type_hints(method).get(parameter_names[1])


def test_evidence_answer_boundaries_accept_request_without_previous_answer() -> None:
    assert (
        tuple(inspect.signature(EvidenceAnswerDraftGenerator.stream).parameters),
        tuple(inspect.signature(EvidenceAnswerer.answer).parameters),
        tuple(inspect.signature(EvidenceAnswerFlow.answer).parameters),
        _first_input_annotation(EvidenceAnswerDraftGenerator.stream),
        _first_input_annotation(EvidenceAnswerer.answer),
        _first_input_annotation(EvidenceAnswerFlow.answer),
    ) == (
        ("self", "request", "evidence", "target_time_window", "previous_error"),
        ("self", "request", "evidence", "target_time_window"),
        ("self", "request", "evidence", "target_time_window"),
        AnsweringRequest,
        AnsweringRequest,
        AnsweringRequest,
    )


def test_raw_draft_accepts_lenient_provider_values() -> None:
    draft = RawEvidenceAnswerDraft(
        sufficiency=1,
        answer=None,
        cited_refs=["1", 2, None],
        missing_aspects=["一次情報", False],
    )

    assert draft.sufficiency == 1
    assert draft.answer is None
    assert draft.cited_refs == ["1", 2, None]
    assert draft.missing_aspects == ["一次情報", False]


@pytest.mark.parametrize(
    "draft",
    [
        {
            "sufficiency": "answered",
            "answer": "回答です。[[1]]",
            "cited_refs": [],
        },
        {
            "sufficiency": "answered",
            "answer": "回答です。[[1]]",
            "cited_refs": ["1"],
            "missing_aspects": ["不足"],
        },
        {
            "sufficiency": "insufficient",
            "answer": "不足しています。",
            "missing_aspects": [],
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
