"""Evidence answer のEOF後JSON parser契約。"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import cast

import pytest

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraftGenerationInvalidError,
    RawEvidenceAnswerDraft,
)

_NOT_JSON = "evidence_answer_response_gemini_not_json"
_NOT_OBJECT = "evidence_answer_response_gemini_not_object"
_DUPLICATE_TOP_LEVEL = "evidence_answer_response_duplicate_top_level_key"


def _parser() -> Callable[[str], RawEvidenceAnswerDraft]:
    try:
        module = import_module("app.agent.answering.evidence_answer.final_json")
    except ModuleNotFoundError as exc:
        if exc.name != "app.agent.answering.evidence_answer.final_json":
            raise
        pytest.fail("Evidence answer の最終JSON parserが未実装です", pytrace=False)

    parser = getattr(module, "parse_evidence_answer_final_json", None)
    assert parser is not None, "parse_evidence_answer_final_json が未実装です"
    return cast("Callable[[str], RawEvidenceAnswerDraft]", parser)


def test_valid_root_object_becomes_existing_raw_draft() -> None:
    raw_json = """{
      "answer": "根拠から確認できます。[[1]]",
      "cited_refs": ["1"],
      "missing_aspects": [],
      "sufficiency": "answered"
    }"""

    draft = _parser()(raw_json)

    assert draft == RawEvidenceAnswerDraft(
        sufficiency="answered",
        answer="根拠から確認できます。[[1]]",
        cited_refs=["1"],
        missing_aspects=[],
    )


@pytest.mark.parametrize("raw_json", ["not json", "{", ""])
def test_non_json_uses_existing_typed_defect(raw_json: str) -> None:
    with pytest.raises(EvidenceAnswerDraftGenerationInvalidError) as exc_info:
        _parser()(raw_json)

    assert exc_info.value.defect_code == _NOT_JSON


@pytest.mark.parametrize("raw_json", ["[]", '"text"', "1", "null"])
def test_non_object_root_uses_existing_typed_defect(raw_json: str) -> None:
    with pytest.raises(EvidenceAnswerDraftGenerationInvalidError) as exc_info:
        _parser()(raw_json)

    assert exc_info.value.defect_code == _NOT_OBJECT


@pytest.mark.parametrize(
    "raw_json",
    [
        '{"answer":"first","answer":"last"}',
        '{"sufficiency":"answered","sufficiency":"insufficient"}',
        '{"cited_refs":["1"],"cited_refs":["2"]}',
        '{"missing_aspects":[],"missing_aspects":["unknown"]}',
        '{"future_field":1,"future_field":2}',
    ],
    ids=("answer", "sufficiency", "cited-refs", "missing-aspects", "unknown"),
)
def test_every_top_level_duplicate_key_uses_fixed_defect(raw_json: str) -> None:
    with pytest.raises(EvidenceAnswerDraftGenerationInvalidError) as exc_info:
        _parser()(raw_json)

    assert exc_info.value.defect_code == _DUPLICATE_TOP_LEVEL


def test_nested_duplicate_keys_are_outside_this_parser_rejection_scope() -> None:
    raw_json = """{
      "sufficiency": "insufficient",
      "answer": "確認できる範囲を回答します。",
      "cited_refs": [],
      "missing_aspects": ["一次情報"],
      "metadata": {
        "answer": "nested first",
        "answer": "nested last",
        "value": 1,
        "value": 2
      }
    }"""

    draft = _parser()(raw_json)

    assert draft == RawEvidenceAnswerDraft(
        sufficiency="insufficient",
        answer="確認できる範囲を回答します。",
        cited_refs=[],
        missing_aspects=["一次情報"],
    )
