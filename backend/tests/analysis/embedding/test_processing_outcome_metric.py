"""Embedding処理結果メトリクスの件数と属性の契約。"""

from __future__ import annotations

from typing import get_args

import pytest
from logfire.testing import CaptureLogfire

from app.analysis.embedding.metrics import (
    EmbeddingProcessingOutcome,
    record_embedding_processing_outcome,
)
from tests.logfire._metric_helpers import (
    assert_attribute_contract,
    collected_metrics,
    sum_counter_for_result,
)

_METRIC = "vector.embedding.processing_outcome"
_ALL_RESULTS = get_args(EmbeddingProcessingOutcome)


# helper 契約: 3 値それぞれを 1 件として記録する


@pytest.mark.parametrize("result", _ALL_RESULTS)
def test_record_emits_one_count_for_each_result(
    capfire: CaptureLogfire, result: str
) -> None:
    """record_embedding_processing_outcome(v) で result=v が +1、他値は 0。"""
    record_embedding_processing_outcome(result)  # type: ignore[arg-type]
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, result) == 1
    for other in (r for r in _ALL_RESULTS if r != result):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


# attribute contract: result key のみ、値は宣言された語彙のみ


def test_attributes_conform_to_declared_vocabulary(capfire: CaptureLogfire) -> None:
    """counter の attribute が {"result"} key のみで、値は EmbeddingProcessingOutcome
    の語彙内。
    """
    record_embedding_processing_outcome("succeeded")
    metrics = collected_metrics(capfire)
    assert_attribute_contract(metrics, _METRIC, allowed={"result": _ALL_RESULTS})
