"""``TrendDiscoveryPayload`` の schema validation / discriminator pin test。"""

from __future__ import annotations

from pydantic import TypeAdapter

from app.audit.domain.payloads import PipelineEventPayload, TrendDiscoveryPayload


def test_kind_is_trend_discovery_default() -> None:
    """instance default の ``kind`` が ``"trend_discovery"`` であること。"""
    payload = TrendDiscoveryPayload()
    assert payload.kind == "trend_discovery"


def test_parses_via_trend_discovery_discriminator() -> None:
    """``kind="trend_discovery"`` の dict が union 経由で parse される。"""
    adapter: TypeAdapter[PipelineEventPayload] = TypeAdapter(PipelineEventPayload)
    parsed = adapter.validate_python(
        {
            "kind": "trend_discovery",
            "window_start": "2026-04-26",
            "window_end": "2026-05-03",
            "trigger": "cron",
            "requested_update": False,
        }
    )
    assert isinstance(parsed, TrendDiscoveryPayload)


def test_full_trend_discovery_payload_roundtrip() -> None:
    """run fields を含む JSONB dump → validate が同値で戻る。"""
    original = TrendDiscoveryPayload(
        window_start="2026-04-26",
        window_end="2026-05-03",
        trigger="cli",
        requested_update=True,
        source_analysis_count=42,
        completed_category_count=3,
        error_message="select failed",
        error_chain=["builtins.RuntimeError"],
    )
    dumped = original.model_dump(mode="json", exclude_none=False)
    restored = TrendDiscoveryPayload.model_validate(dumped)
    assert restored == original
