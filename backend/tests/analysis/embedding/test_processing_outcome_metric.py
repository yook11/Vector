"""``vector.embedding.processing_outcome`` counter の不変条件 (正本)。

embedding 処理試行の結末を集計する metric。インフラ障害 (infra_error) を成功率の分母から
外して可視化するため、span result の影ではなく分類が判明する境界で emit する。本ファイル
は helper の emit 契約と、span backstop が counter を汚さないことを固定する
(emit 境界ごとの分類は service / task / handler の各テストが正本)。
"""

from __future__ import annotations

from typing import get_args

import pytest
from logfire.testing import CaptureLogfire

from app.analysis.embedding.metrics import (
    EmbeddingProcessingOutcome,
    record_embedding_processing_outcome,
)
from app.logfire.article_stage import embedding_stage_span
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


# backstop は span result=failed を焼くが processing_outcome は emit しない


def test_backstop_failed_does_not_emit_processing_outcome(
    capfire: CaptureLogfire,
) -> None:
    """result 未設定で例外貫通 → backstop の failed は counter を汚さない。"""
    with pytest.raises(ValueError, match="boom"):
        with embedding_stage_span(analyzed_article_id=1):
            raise ValueError("boom")
    metrics = collected_metrics(capfire)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(metrics, _METRIC, result) == 0


# attribute contract: result key のみ、値は宣言された語彙のみ


def test_attributes_conform_to_declared_vocabulary(capfire: CaptureLogfire) -> None:
    """counter の attribute が {"result"} key のみで、値は EmbeddingProcessingOutcome
    の語彙内。
    """
    record_embedding_processing_outcome("succeeded")
    metrics = collected_metrics(capfire)
    assert_attribute_contract(metrics, _METRIC, allowed={"result": _ALL_RESULTS})
