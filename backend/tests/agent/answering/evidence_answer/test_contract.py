"""Evidence answer contract tests."""

import inspect
from dataclasses import FrozenInstanceError, fields
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
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


def test_evidence_answer_boundaries_accept_request_without_previous_answer() -> None:
    """条件7: EvidenceAnswerer.answer()にreview_missingキーワード引数が増える

    (AnsweringRunnerがoutcome.review.missingを渡す受け渡し口。
    受け渡しの責務はrunnerにあり、flowはこの引数を受け取るだけ)。
    """
    assert (
        tuple(inspect.signature(EvidenceAnswerer.answer).parameters),
        tuple(inspect.signature(EvidenceAnswerService.answer).parameters),
        _first_input_annotation(EvidenceAnswerer.answer),
        _first_input_annotation(EvidenceAnswerService.answer),
    ) == (
        ("self", "request", "evidence", "target_time_window", "review_missing"),
        ("self", "request", "evidence", "target_time_window", "review_missing"),
        AnsweringRequest,
        AnsweringRequest,
    )


def test_review_missing_is_a_required_keyword_argument_on_answer() -> None:
    """コーディネーター判断(S5追補): review_missingはEvidenceAnswerer.answer()/

    EvidenceAnswerService.answer()の必須キーワード引数にする(デフォルトは
    EvidenceAnswerInput.review_missing側にだけ残す)。渡し忘れがInputの
    既定()で黙って握りつぶされないよう、呼び出し側に必ず判断を迫る
    (責任境界表: review.missingの受け渡しはAnsweringRunnerの単独責務)。
    """
    protocol_params = inspect.signature(EvidenceAnswerer.answer).parameters
    service_params = inspect.signature(EvidenceAnswerService.answer).parameters

    assert "review_missing" in protocol_params
    assert "review_missing" in service_params
    assert protocol_params["review_missing"].default is inspect.Parameter.empty
    assert service_params["review_missing"].default is inspect.Parameter.empty


def test_evidence_answer_input_is_frozen_and_keeps_attempt_state_together() -> None:
    assert [field.name for field in fields(EvidenceAnswerInput)] == [
        "request",
        "evidence",
        "target_time_window",
        "repair_context",
        "previous_output_truncated",
        "review_missing",
    ]
    type_hints = get_type_hints(EvidenceAnswerInput)
    assert type_hints["target_time_window"] == TargetTimeWindow | None
    assert type_hints.get("review_missing") == tuple[str, ...]
    input = EvidenceAnswerInput(
        request=object(),
        evidence=(),
        target_time_window=None,
    )
    assert input.repair_context is None
    assert input.previous_output_truncated is False
    assert input.review_missing == ()
    with pytest.raises(FrozenInstanceError):
        input.target_time_window = TargetTimeWindow(kind="today")


def test_evidence_answer_draft_has_only_answer_and_cited_refs() -> None:
    """条件1・21: 出力draftは本文とcited_refsだけを持つ (sufficiency /
    missing_aspects / unfulfilled_requirement_idsは撤去される)。
    """
    assert set(EvidenceAnswerDraft.model_fields) == {"answer", "cited_refs"}


def test_strict_draft_rejects_blank_answer() -> None:
    """answer: NonBlankTextの型制約は維持する (model単体構築時のpydantic契約)。"""
    with pytest.raises(ValidationError):
        EvidenceAnswerDraft(answer="   ", cited_refs=[])
