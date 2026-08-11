"""Evidence answer contract tests."""

import inspect
from dataclasses import FrozenInstanceError, fields
from importlib import import_module, util
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerer,
)
from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow
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
        tuple(inspect.signature(EvidenceAnswerFlow.answer).parameters),
        _first_input_annotation(EvidenceAnswerer.answer),
        _first_input_annotation(EvidenceAnswerFlow.answer),
    ) == (
        ("self", "request", "evidence", "target_time_window", "review_missing"),
        ("self", "request", "evidence", "target_time_window", "review_missing"),
        AnsweringRequest,
        AnsweringRequest,
    )


def test_review_missing_is_a_required_keyword_argument_on_answer() -> None:
    """コーディネーター判断(S5追補): review_missingはEvidenceAnswerer.answer()/

    EvidenceAnswerFlow.answer()の必須キーワード引数にする(デフォルトは
    EvidenceAnswerInput.review_missing側にだけ残す)。渡し忘れがInputの
    既定()で黙って握りつぶされないよう、呼び出し側に必ず判断を迫る
    (責任境界表: review.missingの受け渡しはAnsweringRunnerの単独責務)。
    """
    protocol_params = inspect.signature(EvidenceAnswerer.answer).parameters
    flow_params = inspect.signature(EvidenceAnswerFlow.answer).parameters

    assert "review_missing" in protocol_params
    assert "review_missing" in flow_params
    assert protocol_params["review_missing"].default is inspect.Parameter.empty
    assert flow_params["review_missing"].default is inspect.Parameter.empty


def test_evidence_answer_input_is_frozen_and_keeps_attempt_state_together() -> None:
    contract = import_module("app.agent.answering.evidence_answer.contract")
    input_type = getattr(contract, "EvidenceAnswerInput", None)

    assert input_type is not None, "EvidenceAnswerInput が未実装です"
    assert [field.name for field in fields(input_type)] == [
        "request",
        "evidence",
        "target_time_window",
        "repair_context",
        "previous_output_truncated",
        "review_missing",
    ]
    type_hints = get_type_hints(input_type)
    assert type_hints["target_time_window"] == TargetTimeWindow | None
    assert type_hints.get("review_missing") == tuple[str, ...]
    input = input_type(
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


def test_contract_module_does_not_export_removed_raw_json_symbols() -> None:
    """条件21: evidence_answer/はRawEvidenceAnswerDraftを持たない。
    EvidenceAnswerSufficiencyも無い。

    R3条件11: EvidenceAnswerDraftGenerationInvalidError(唯一のproducerだった
    parse_evidence_answer_final_json()がS4で削除され到達不能)も無い。
    app.agent.answering(親package)からもre-exportされないことを確認する。
    """
    contract = import_module("app.agent.answering.evidence_answer.contract")
    answering_package = import_module("app.agent.answering")

    assert getattr(contract, "RawEvidenceAnswerDraft", None) is None
    assert getattr(contract, "EvidenceAnswerSufficiency", None) is None
    assert getattr(contract, "EvidenceAnswerDraftGenerationInvalidError", None) is None
    assert (
        getattr(answering_package, "EvidenceAnswerDraftGenerationInvalidError", None)
        is None
    )
    assert "EvidenceAnswerDraftGenerationInvalidError" not in answering_package.__all__


def test_json_answer_extractor_module_does_not_exist() -> None:
    """条件20: json_answer_extractor.pyがリポジトリに存在しない。"""
    assert (
        util.find_spec("app.agent.answering.evidence_answer.json_answer_extractor")
        is None
    )


def test_final_json_module_does_not_exist() -> None:
    """条件20: final_json.pyがリポジトリに存在しない。"""
    assert util.find_spec("app.agent.answering.evidence_answer.final_json") is None
