"""``ExtractionCall`` envelope の振る舞いテスト (PR3-a-1)。

確認する性質:
- 3 field (result / raw_response / prompt_version) を持つ
- ``frozen=True`` で生成後の代入を拒否
- ``slots=True`` で ``__dict__`` を持たない
"""

from __future__ import annotations

import dataclasses

import pytest

from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.domain import ExtractedEntity, ExtractionResult


def _result() -> ExtractionResult:
    return ExtractionResult(
        relevance="signal",
        title_ja="t",
        summary_ja="s",
        entities=[
            ExtractedEntity(
                surface=EntitySurface("X"), raw_type=EntityRawType("Company")
            )
        ],
    )


def test_fields_are_result_raw_response_prompt_version() -> None:
    fields = {f.name for f in dataclasses.fields(ExtractionCall)}
    assert fields == {"result", "raw_response", "prompt_version"}


def test_construct_and_read_fields() -> None:
    call = ExtractionCall(
        result=_result(), raw_response='{"x": 1}', prompt_version="abcdef01"
    )
    assert call.result.relevance == "signal"
    assert call.raw_response == '{"x": 1}'
    assert call.prompt_version == "abcdef01"


def test_frozen_disallows_mutation() -> None:
    call = ExtractionCall(
        result=_result(), raw_response="raw", prompt_version="abcdef01"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.raw_response = "mutated"  # type: ignore[misc]


def test_slots_excludes_dict() -> None:
    """``slots=True`` で ``__dict__`` を持たない (構造的に余計な属性を持てない)。"""
    call = ExtractionCall(
        result=_result(), raw_response="raw", prompt_version="abcdef01"
    )
    assert not hasattr(call, "__dict__")
    assert ExtractionCall.__slots__ == ("result", "raw_response", "prompt_version")
