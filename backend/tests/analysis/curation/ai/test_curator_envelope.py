"""``CurationCall`` envelope の振る舞いテスト (PR1-a Generic 化対応)。

確認する性質:
- 5 field (result / raw_response / raw_relevance / prompt_version / model_name)
  を持つ
- ``frozen=True`` で生成後の代入を拒否
- ``slots=True`` で ``__dict__`` を持たない
- ``Signal`` / ``Noise`` のいずれも ``result`` に詰められる (Generic narrow 経由)
"""

from __future__ import annotations

import dataclasses

import pytest

from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.domain import Noise, Signal


def _signal() -> Signal:
    return Signal(title_ja="t", summary_ja="s")


def _noise() -> Noise:
    return Noise(title_ja="t", summary_ja="s")


def test_fields_include_model_name_and_raw_relevance() -> None:
    fields = {f.name for f in dataclasses.fields(CurationCall)}
    assert fields == {
        "result",
        "raw_response",
        "raw_relevance",
        "prompt_version",
        "model_name",
    }


def test_construct_and_read_signal_envelope_fields() -> None:
    call: CurationCall[Signal] = CurationCall(
        result=_signal(),
        raw_response='{"x": 1}',
        raw_relevance="signal",
        prompt_version="abcdef01",
        model_name="gemini-2.5-flash-lite",
    )
    assert isinstance(call.result, Signal)
    assert call.raw_response == '{"x": 1}'
    assert call.raw_relevance == "signal"
    assert call.prompt_version == "abcdef01"
    assert call.model_name == "gemini-2.5-flash-lite"


def test_construct_and_read_noise_envelope_fields() -> None:
    call: CurationCall[Noise] = CurationCall(
        result=_noise(),
        raw_response='{"x": 2}',
        raw_relevance="noise",
        prompt_version="abcdef01",
        model_name="gemini-2.5-flash-lite",
    )
    assert isinstance(call.result, Noise)
    assert call.raw_relevance == "noise"


def test_frozen_disallows_mutation() -> None:
    call = CurationCall(
        result=_signal(),
        raw_response="raw",
        raw_relevance="signal",
        prompt_version="abcdef01",
        model_name="gemini-2.5-flash-lite",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.raw_response = "mutated"  # type: ignore[misc]


def test_slots_excludes_dict() -> None:
    """``slots=True`` で ``__dict__`` を持たない (構造的に余計な属性を持てない)。"""
    call = CurationCall(
        result=_signal(),
        raw_response="raw",
        raw_relevance="signal",
        prompt_version="abcdef01",
        model_name="gemini-2.5-flash-lite",
    )
    assert not hasattr(call, "__dict__")
    assert CurationCall.__slots__ == (
        "result",
        "raw_response",
        "raw_relevance",
        "prompt_version",
        "model_name",
    )
