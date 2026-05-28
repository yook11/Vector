"""``BackfillPayload`` の schema validation / discriminator pin test。"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.audit.domain.payloads import BackfillPayload, PipelineEventPayload


def test_kind_is_backfill_default() -> None:
    """instance default の ``kind`` が ``"backfill"`` であること。"""
    payload = BackfillPayload(backfill_stage="assess")
    assert payload.kind == "backfill"


def test_parses_via_backfill_discriminator() -> None:
    """``kind="backfill"`` の dict が union 経由で BackfillPayload になる。"""
    adapter: TypeAdapter[PipelineEventPayload] = TypeAdapter(PipelineEventPayload)
    parsed = adapter.validate_python({"kind": "backfill", "backfill_stage": "embed"})
    assert isinstance(parsed, BackfillPayload)


def test_backfill_stage_is_required() -> None:
    """stage 不明の backfill payload は受理しない。"""
    adapter: TypeAdapter[PipelineEventPayload] = TypeAdapter(PipelineEventPayload)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "backfill"})


def test_full_backfill_payload_roundtrip() -> None:
    """run / item fields を含む JSONB dump → validate が同値で戻る。"""
    original = BackfillPayload(
        backfill_stage="assess",
        run_id="run-1",
        target_kind="curation",
        target_id=42,
        source_name="VentureBeat",
        selected_count=10,
        granted_count=5,
        enqueued_count=4,
        failed_count=1,
        limit=50,
        daily_max=600,
        error_message="queue down",
        error_chain=["builtins.RuntimeError"],
    )
    dumped = original.model_dump(mode="json", exclude_none=False)
    restored = BackfillPayload.model_validate(dumped)
    assert restored == original
