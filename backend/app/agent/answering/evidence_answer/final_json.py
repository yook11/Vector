"""Evidence answerの完全JSONをraw draftへ変換する。"""

from __future__ import annotations

import json

from pydantic import BaseModel

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraftGenerationInvalidError,
)

_NOT_JSON = "evidence_answer_response_gemini_not_json"
_NOT_OBJECT = "evidence_answer_response_gemini_not_object"
_DUPLICATE_TOP_LEVEL_KEY = "evidence_answer_response_duplicate_top_level_key"


class _JsonObjectPairs(list[tuple[str, object]]):
    pass


def parse_evidence_answer_final_json[OutputT: BaseModel](
    raw_json: str,
    *,
    output_type: type[OutputT],
) -> OutputT:
    """root keyの一意性を保ち、宣言されたoutput typeで検証する。"""
    try:
        parsed = json.loads(raw_json, object_pairs_hook=_JsonObjectPairs)
    except json.JSONDecodeError as exc:
        raise EvidenceAnswerDraftGenerationInvalidError(_NOT_JSON) from exc

    if not isinstance(parsed, _JsonObjectPairs):
        raise EvidenceAnswerDraftGenerationInvalidError(_NOT_OBJECT)

    seen_keys: set[str] = set()
    payload: dict[str, object] = {}
    for key, value in parsed:
        if key in seen_keys:
            raise EvidenceAnswerDraftGenerationInvalidError(_DUPLICATE_TOP_LEVEL_KEY)
        seen_keys.add(key)
        payload[key] = _restore_json_value(value)

    return output_type.model_validate(payload)


def _restore_json_value(value: object) -> object:
    if isinstance(value, _JsonObjectPairs):
        return {key: _restore_json_value(item) for key, item in value}
    if isinstance(value, list):
        return [_restore_json_value(item) for item in value]
    return value
