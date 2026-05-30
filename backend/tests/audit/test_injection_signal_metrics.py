"""``app/audit/injection_signal.py`` の metric 記録 oracle。

検証する性質:
- ``record_injection_boundary_detected`` 1 回で counter
  (``vector.audit.injection_boundary_detected``) が +1。
- attribute は ``{"stage": <stage>}`` 1 key 固定 (低 cardinality)。
- dump 全体に stage 以外の dynamic 値が混入しない (PII 非含有 oracle)。

設計スタンス:
- capfire fixture が ``logfire.configure(...)`` を自前で呼ぶため本テストでは
  ``setup_logfire`` を呼ばない (二重 configure 回避)。
- ヘルパは ``test_maintenance_age_delete_metrics.py`` と同形 (module 跨ぎで複製、
  共通化は「同じ問題」検出時に括る)。
"""

from __future__ import annotations

import json
from typing import Any

from logfire.testing import CaptureLogfire

from app.audit.injection_signal import record_injection_boundary_detected

_METRIC = "vector.audit.injection_boundary_detected"


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _sum_value(metric: dict[str, Any]) -> int:
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


def test_record_increments_counter_once(capfire: CaptureLogfire) -> None:
    """1 回の記録で counter は +1。"""
    record_injection_boundary_detected(stage="completion")

    metric = _find_metric(capfire.get_collected_metrics(), _METRIC)
    assert metric is not None
    assert _sum_value(metric) == 1


def test_attribute_is_stage_only(capfire: CaptureLogfire) -> None:
    """attribute は ``{"stage": ...}`` 1 key 固定。"""
    record_injection_boundary_detected(stage="curation")

    metric = _find_metric(capfire.get_collected_metrics(), _METRIC)
    assert metric is not None
    attrs_list = _attributes_for(metric)
    assert attrs_list == [{"stage": "curation"}]


def test_metric_dump_carries_no_dynamic_value(capfire: CaptureLogfire) -> None:
    """stage 値以外の動的値が attribute / dump に混入しない (PII oracle)。

    将来 article_id / URL を attribute に足す regression が起きれば本 oracle が
    落ちる (`feedback_per_seam_mapping_totality_oracle`)。
    """
    distinctive = "completion"
    record_injection_boundary_detected(stage=distinctive)

    metrics = capfire.get_collected_metrics()
    metric = _find_metric(metrics, _METRIC)
    assert metric is not None
    for attrs in _attributes_for(metric):
        assert set(attrs.keys()) == {"stage"}
    # 高 cardinality / PII な per-event 識別子が attribute / dump に混入しないこと。
    # article_id / canonical_url / source_id を attribute に足す regression が起きれば
    # dump 文字列の部分一致で本 oracle が実際に落ちる (それ以前は空虚な恒真だった)。
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    assert "article_id" not in dumped
    assert "canonical_url" not in dumped
    assert "source_id" not in dumped
