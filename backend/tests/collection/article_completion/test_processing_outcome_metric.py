"""``vector.completion.processing_outcome`` counter の不変条件 (正本)。

completion 処理試行の結末を集計する metric。infra_error (一時的) を成功率の分母から
外して可視化する。本ファイルは helper の emit 契約と attribute 安全性を固定する
(emit 境界ごとの分類は service / handler / task の各テストが正本)。completion には
``article_stage`` span が無いため backstop テストは持たない。
"""

from __future__ import annotations

from typing import get_args

import pytest
from logfire.testing import CaptureLogfire

from app.collection.article_completion.metrics import (
    CompletionProcessingOutcome,
    record_completion_processing_outcome,
)
from tests.logfire._metric_helpers import (
    assert_attribute_contract,
    collected_metrics,
    sum_counter_for_result,
)

_METRIC = "vector.completion.processing_outcome"
_ALL_RESULTS = get_args(CompletionProcessingOutcome)


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


# attribute contract: result key のみ、値は宣言された語彙のみ


def test_attributes_conform_to_declared_vocabulary(capfire: CaptureLogfire) -> None:
    """counter の attribute が {"result"} key のみで、値は CompletionProcessingOutcome
    の語彙内。
    """
    record_completion_processing_outcome("succeeded")
    metrics = collected_metrics(capfire)
    assert_attribute_contract(metrics, _METRIC, allowed={"result": _ALL_RESULTS})
