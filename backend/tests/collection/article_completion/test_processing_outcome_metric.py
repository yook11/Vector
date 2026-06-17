"""``vector.completion.processing_outcome`` counter の不変条件 (正本)。

completion 処理試行の結末を集計する metric。infra_error (一時的) を成功率の分母から
外して可視化する。本ファイルは helper の emit 契約と attribute 安全性を固定する
(emit 境界ごとの分類は service / handler / task の各テストが正本)。completion には
``article_stage`` span が無いため backstop テストは持たない。
"""

from __future__ import annotations

import json

import pytest
from logfire.testing import CaptureLogfire

from app.collection.article_completion.metrics import (
    record_completion_processing_outcome,
)
from tests.logfire._metric_helpers import (
    collected_metrics,
    counter_attribute_key_sets,
    sum_counter_for_result,
)

_METRIC = "vector.completion.processing_outcome"
_ALL_RESULTS = ("succeeded", "failed", "infra_error")


# helper 契約: 3 値それぞれを 1 件として記録する


@pytest.mark.parametrize("result", _ALL_RESULTS)
def test_record_emits_one_count_for_each_result(
    capfire: CaptureLogfire, result: str
) -> None:
    """record_completion_processing_outcome(v) で result=v が +1、他値は 0。"""
    record_completion_processing_outcome(result)  # type: ignore[arg-type]
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, result) == 1
    for other in (r for r in _ALL_RESULTS if r != result):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


# attribute safety: result のみ、PII 非含有


def test_attribute_is_result_only_no_pii(capfire: CaptureLogfire) -> None:
    """counter の全 data point attribute keys が {"result"} のみで PII を載せない。"""
    record_completion_processing_outcome("succeeded")
    metrics = collected_metrics(capfire)
    key_sets = counter_attribute_key_sets(metrics, _METRIC)
    assert key_sets == [{"result"}], f"unexpected attribute keys: {key_sets}"
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    for needle in (
        "incomplete_article_id",
        "analyzable_article_id",
        "source_id",
        "http://",
        "https://",
        "status_code",
        "reason_code",
        "body_sample",
        "failure_kind",
    ):
        assert needle not in dumped, f"PII 様文字列 {needle!r} が metric dump に混入"
