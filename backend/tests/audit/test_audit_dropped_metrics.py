"""``app.audit.metrics.record_audit_dropped`` の metric 記録 oracle。

検証する性質 (兄弟 ``test_injection_signal_metrics.py`` と同形):
- 1 回の記録で counter (``vector.audit.dropped``) が +1。
- attribute は ``{"stage": <wire値>}`` 1 key 固定 (低 cardinality, I3)。
- dump 全体に stage 以外の動的値 (ID 等) が混入しない (PII 非含有 oracle)。

capfire が logfire.configure を自前で呼ぶため setup_logfire は呼ばない。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.audit.domain.event import Stage
from app.audit.metrics import record_audit_dropped

_METRIC = "vector.audit.dropped"


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _sum_value(metric: dict[str, Any]) -> int:
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


def test_record_increments_counter_once(capfire: CaptureLogfire) -> None:
    """1 回の記録で counter は +1。"""
    record_audit_dropped(Stage.CURATION)

    metric = _find_metric(capfire.get_collected_metrics(), _METRIC)
    assert metric is not None
    assert _sum_value(metric) == 1


@pytest.mark.parametrize(
    ("stage", "wire"),
    # wire 値は Stage の DB CHECK 値 (app/audit/domain/event.py の SSoT)。
    [
        (Stage.CURATION, "curation"),
        (Stage.DISPATCH, "dispatch"),
        (Stage.BACKFILL_EMBED, "backfill_embed"),
    ],
)
def test_attribute_is_stage_only(
    capfire: CaptureLogfire, stage: Stage, wire: str
) -> None:
    """attribute は ``{"stage": <wire値>}`` 1 key 固定 (ID は載らない)。"""
    record_audit_dropped(stage)

    metric = _find_metric(capfire.get_collected_metrics(), _METRIC)
    assert metric is not None
    assert _attributes_for(metric) == [{"stage": wire}]


def test_metric_dump_carries_no_dynamic_value(capfire: CaptureLogfire) -> None:
    """stage 以外の動的値 (article_id / URL / source_id) が dump に混入しない。

    ID label を足す regression が起きれば dump の部分一致で本 oracle が落ちる
    (`feedback_per_seam_mapping_totality_oracle`)。
    """
    record_audit_dropped(Stage.ACQUISITION)

    metrics = capfire.get_collected_metrics()
    metric = _find_metric(metrics, _METRIC)
    assert metric is not None
    for attrs in _attributes_for(metric):
        assert set(attrs.keys()) == {"stage"}
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    assert "article_id" not in dumped
    assert "canonical_url" not in dumped
    assert "source_id" not in dumped
