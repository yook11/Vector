"""``vector.assessment.processing_outcome`` counter の不変条件 (正本)。

assessment 処理試行の結末を集計する metric。インフラ障害 (infra_error) を成功率の
分母から外して可視化するため、span result の影ではなく分類が判明する境界で emit する。
本ファイルは helper の emit 契約と、span backstop が counter を汚さないことを固定する
(emit 境界ごとの分類は service / task / handler の各テストが正本)。
"""

from __future__ import annotations

import json

import pytest
from logfire.testing import CaptureLogfire

from app.analysis.assessment.metrics import record_assessment_processing_outcome
from app.logfire.article_stage import assessment_stage_span
from tests.logfire._metric_helpers import (
    collected_metrics,
    counter_attribute_key_sets,
    sum_counter_for_result,
)

_METRIC = "vector.assessment.processing_outcome"
_ALL_RESULTS = ("in_scope", "out_of_scope", "failed", "infra_error")


# helper 契約: 4 値それぞれを 1 件として記録する


@pytest.mark.parametrize("result", _ALL_RESULTS)
def test_record_emits_one_count_for_each_result(
    capfire: CaptureLogfire, result: str
) -> None:
    """record_assessment_processing_outcome(v) で result=v が +1、他値は 0。"""
    record_assessment_processing_outcome(result)  # type: ignore[arg-type]
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, result) == 1
    for other in (r for r in _ALL_RESULTS if r != result):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


# backstop は span result=failed を焼くが processing_outcome は emit しない


def test_backstop_failed_does_not_emit_processing_outcome(
    capfire: CaptureLogfire,
) -> None:
    """result 未設定で例外貫通 → backstop の failed は counter を汚さない。"""
    with pytest.raises(ValueError, match="boom"):
        with assessment_stage_span(curation_id=1):
            raise ValueError("boom")
    metrics = collected_metrics(capfire)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(metrics, _METRIC, result) == 0


# attribute safety: result のみ、PII 非含有


def test_attribute_is_result_only_no_pii(capfire: CaptureLogfire) -> None:
    """counter の全 data point attribute keys が {"result"} のみで PII を載せない。"""
    record_assessment_processing_outcome("in_scope")
    metrics = collected_metrics(capfire)
    key_sets = counter_attribute_key_sets(metrics, _METRIC)
    assert key_sets == [{"result"}], f"unexpected attribute keys: {key_sets}"
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    for needle in (
        "curation_id",
        "analyzable_article_id",
        "source_id",
        "http://",
        "https://",
        "prompt",
        "raw_response",
    ):
        assert needle not in dumped, f"PII 様文字列 {needle!r} が metric dump に混入"
