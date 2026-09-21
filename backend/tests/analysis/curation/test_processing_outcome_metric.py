"""``vector.curation.processing_outcome`` counter の不変条件 (正本)。

curationの処理結果を集計し、Consumerの失敗は原因によらずfailedとして数える。
本ファイルは helper の emit 契約と、span backstop が counter を汚さないことを固定する
(emit 境界ごとの分類は service / task / handler の各テストが正本)。

capfire は内部で ``logfire.configure(send_to_logfire=False, ...)`` を呼ぶため
setup_logfire は呼ばない。
"""

from __future__ import annotations

from typing import get_args

import pytest
from logfire.testing import CaptureLogfire

from app.analysis.curation.metrics import (
    CurationProcessingOutcome,
    record_curation_processing_outcome,
)
from tests.logfire._metric_helpers import (
    assert_attribute_contract,
    collected_metrics,
    sum_counter_for_result,
)

_METRIC = "vector.curation.processing_outcome"
_ALL_RESULTS = get_args(CurationProcessingOutcome)


# helper 契約: 4 値それぞれを 1 件として記録する


@pytest.mark.parametrize("result", _ALL_RESULTS)
def test_record_emits_one_count_for_each_result(
    capfire: CaptureLogfire, result: str
) -> None:
    """record_curation_processing_outcome(v) で result=v が +1、他値は 0。"""
    record_curation_processing_outcome(result)  # type: ignore[arg-type]
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, result) == 1
    for other in (r for r in _ALL_RESULTS if r != result):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


# attribute contract: result key のみ、値は宣言された語彙のみ


def test_attributes_conform_to_declared_vocabulary(capfire: CaptureLogfire) -> None:
    """counter の attribute が {"result"} key のみで、値は CurationProcessingOutcome
    の語彙内。
    """
    record_curation_processing_outcome("signal")
    metrics = collected_metrics(capfire)
    assert_attribute_contract(metrics, _METRIC, allowed={"result": _ALL_RESULTS})
