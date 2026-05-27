"""``AssessmentPayload`` の schema validation / discriminator pin test。

PR5 で ``ClassificationPayload`` → ``AssessmentPayload`` に完全置換した
状態を固定する。``tests/observability/domain/test_payloads.py`` の PR4
pin test 3 本の役割を本 file に引き継ぐ:

- discriminator 値が ``"assessment"`` であること
- ``kind="assessment"`` の dict が discriminated union 経由で
  ``AssessmentPayload`` に dispatch されること
- ``kind="classification"`` (PR4 以前の旧値) は ``ValidationError`` で reject
  されること (PR4 後の DB 状態保護を継続)
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.audit.domain.payloads import (
    AssessmentPayload,
    PipelineEventPayload,
)


def test_kind_is_assessment_default() -> None:
    """instance default の ``kind`` が ``"assessment"`` であること。"""
    payload = AssessmentPayload()
    assert payload.kind == "assessment"


def test_kind_field_default_is_assessment() -> None:
    """class 定義の field default が ``"assessment"`` であること。"""
    field_default = AssessmentPayload.model_fields["kind"].default
    assert field_default == "assessment"


def test_parses_via_assessment_discriminator() -> None:
    """``kind="assessment"`` の dict が discriminated union 経由で
    ``AssessmentPayload`` に dispatch される。
    """
    adapter: TypeAdapter[PipelineEventPayload] = TypeAdapter(PipelineEventPayload)
    parsed = adapter.validate_python({"kind": "assessment"})
    assert isinstance(parsed, AssessmentPayload)


def test_rejects_legacy_classification_kind() -> None:
    """PR4 以前の旧値 ``kind="classification"`` を読まない。

    PR4 deploy で既存 row の payload は migration により
    ``kind="assessment"`` に書き換わっているため、PR5 以降の Pydantic
    discriminated union は ``"classification"`` discriminator を受理して
    はならない (DB 状態と schema の一意整合性保証)。
    """
    adapter: TypeAdapter[PipelineEventPayload] = TypeAdapter(PipelineEventPayload)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "classification"})


def test_extra_ignore_drops_unknown_field() -> None:
    """``extra="ignore"`` 継承で未知 field を silent drop。

    rolling deploy 中に新 publisher が焼いた未知 field 付き JSONB を旧 worker が
    ``model_validate`` で読み戻しても ValidationError で死なないことを保証する。
    """
    restored = AssessmentPayload.model_validate(
        {"kind": "assessment", "future_field": "x"}
    )
    assert restored.kind == "assessment"
    assert not hasattr(restored, "future_field")


def test_frozen_immutable() -> None:
    """``BasePipelineEventPayload`` の ``frozen=True`` 継承で mutation 不可。"""
    payload = AssessmentPayload()
    with pytest.raises(ValidationError):
        payload.kind = "x"  # type: ignore[misc]


def test_state_fields_optional_default_none() -> None:
    """``kind`` 以外の全 field が default ``None`` (state 未指定構築可)。"""
    payload = AssessmentPayload()
    # ``kind`` 以外の optional field はすべて None
    assert payload.source_name is None
    assert payload.failure_kind is None
    assert payload.failure_action is None
    assert payload.error_message is None
    assert payload.error_chain is None
    assert payload.curation_id is None
    assert payload.ai_model is None
    assert payload.prompt_version is None
    assert payload.input_text is None
    assert payload.input_text_length is None
    assert payload.ai_raw_response is None
    assert payload.raw_category is None
    assert payload.category_slug is None
    assert payload.investor_take is None


def test_full_in_scope_payload_construction() -> None:
    """全 field を埋めた in-scope 成功状態の構築 (型整合)。"""
    payload = AssessmentPayload(
        source_name="VentureBeat",
        curation_id=42,
        ai_model="gemini-2.5-pro",
        prompt_version="abcd1234",
        input_text="summary text",
        input_text_length=12,
        ai_raw_response='{"category":"ai"}',
        raw_category="ai",
        category_slug="ai",
        investor_take="bullish for inference vendors",
    )
    assert payload.curation_id == 42
    assert payload.category_slug == "ai"
    assert payload.investor_take == "bullish for inference vendors"


def test_input_text_length_int_type() -> None:
    """``input_text_length`` が int 型を受理する。

    Pydantic v2 の strict 化により int field に str を渡すと coercion が
    走るため、明示 int を渡したケースが通ることを最低限固定する。
    """
    payload = AssessmentPayload(input_text_length=4096)
    assert payload.input_text_length == 4096
    assert isinstance(payload.input_text_length, int)


def test_serialization_roundtrip() -> None:
    """JSONB serialization (model_dump → model_validate) で復元可能。

    ``pipeline_events.payload`` は JSONB として永続化されるため、Pydantic v2
    の dump → DB → load 経路が壊れていないことを確認。
    """
    original = AssessmentPayload(
        source_name="VentureBeat",
        curation_id=42,
        ai_model="gemini-2.5-pro",
        category_slug="ai",
        investor_take="bullish",
    )
    dumped = original.model_dump(mode="json", exclude_none=False)
    restored = AssessmentPayload.model_validate(dumped)
    assert restored == original
