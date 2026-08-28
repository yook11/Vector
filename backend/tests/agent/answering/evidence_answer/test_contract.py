"""Evidence answer contract tests."""

import inspect
from dataclasses import MISSING, FrozenInstanceError, fields
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerer,
    EvidenceAnswerInput,
)
from app.agent.answering.evidence_answer.service import EvidenceAnswerService
from app.agent.planning.contract import TargetTimeWindow


def _first_input_annotation(method: object) -> object | None:
    parameter_names = tuple(inspect.signature(method).parameters)
    return get_type_hints(method).get(parameter_names[1])


def test_evidence_answer_boundaries_accept_typed_input() -> None:
    assert (
        tuple(inspect.signature(EvidenceAnswerer.answer).parameters),
        tuple(inspect.signature(EvidenceAnswerService.answer).parameters),
        _first_input_annotation(EvidenceAnswerer.answer),
        _first_input_annotation(EvidenceAnswerService.answer),
    ) == (
        ("self", "input"),
        ("self", "input"),
        EvidenceAnswerInput,
        EvidenceAnswerInput,
    )


def test_review_missing_is_required_on_evidence_answer_input() -> None:
    """渡し忘れが既定()で黙って握りつぶされないよう、呼び出し側に判断を迫る。"""
    review_missing = next(
        field for field in fields(EvidenceAnswerInput) if field.name == "review_missing"
    )
    assert review_missing.default is MISSING
    assert review_missing.default_factory is MISSING


def test_evidence_answer_input_is_frozen_and_keeps_attempt_state_together() -> None:
    assert [field.name for field in fields(EvidenceAnswerInput)] == [
        "request",
        "evidence",
        "target_time_window",
        "review_missing",
        "previous_output_truncated",
    ]
    type_hints = get_type_hints(EvidenceAnswerInput)
    assert type_hints["target_time_window"] == TargetTimeWindow | None
    assert type_hints.get("review_missing") == tuple[str, ...]
    input = EvidenceAnswerInput(
        request=object(),  # type: ignore[arg-type]
        evidence=(),
        target_time_window=None,
        review_missing=(),
    )
    assert input.previous_output_truncated is False
    with pytest.raises(FrozenInstanceError):
        input.target_time_window = TargetTimeWindow(kind="today")  # type: ignore[misc]


def test_evidence_answer_draft_has_only_answer_and_cited_refs() -> None:
    """条件1・21: 出力draftは本文とcited_refsだけを持つ (sufficiency /
    missing_aspects / unfulfilled_requirement_idsは撤去される)。
    """
    assert set(EvidenceAnswerDraft.model_fields) == {"answer", "cited_refs"}


def test_strict_draft_rejects_blank_answer() -> None:
    """answer: NonBlankTextの型制約は維持する (model単体構築時のpydantic契約)。"""
    with pytest.raises(ValidationError):
        EvidenceAnswerDraft(answer="   ", cited_refs=[])
