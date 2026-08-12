"""``processing_outcome`` EMF の call-site 契約 (正本)。

CloudWatch A4 (工程別失敗率 alarm) が消費する stage × result の全系列を
工程横断で 1 か所に固定する。wire format 自体の正本は test_emf.py、各工程の
Logfire counter の不変条件は各 stage の test_processing_outcome_metric.py が持つ。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_args

import pytest

from app.analysis.assessment.metrics import (
    AssessmentProcessingOutcome,
    record_assessment_processing_outcome,
)
from app.analysis.curation.metrics import (
    CurationProcessingOutcome,
    record_curation_processing_outcome,
)
from app.analysis.embedding.metrics import (
    EmbeddingProcessingOutcome,
    record_embedding_processing_outcome,
)
from app.collection.article_completion.metrics import (
    CompletionProcessingOutcome,
    record_completion_processing_outcome,
)
from tests.cloudwatch.records import metric_records

_EMF_METRIC = "processing_outcome"

# (record 関数, stage, result) の全系列。result の値域は各 Literal 型が SSoT。
_SERIES: list[tuple[Callable[[Any], None], str, str]] = [
    (record, stage, result)
    for record, stage, outcome_type in (
        (
            record_completion_processing_outcome,
            "completion",
            CompletionProcessingOutcome,
        ),
        (record_curation_processing_outcome, "curation", CurationProcessingOutcome),
        (
            record_assessment_processing_outcome,
            "assessment",
            AssessmentProcessingOutcome,
        ),
        (record_embedding_processing_outcome, "embedding", EmbeddingProcessingOutcome),
    )
    for result in get_args(outcome_type)
]
_SERIES_IDS = [f"{stage}-{result}" for _, stage, result in _SERIES]


def test_series_total_is_fifteen() -> None:
    """全系列数は 15 (spec のコスト見積もりと alarm 分母定義の前提)。"""
    assert len(_SERIES) == 15


@pytest.mark.parametrize(
    ("record", "result"),
    [(record, result) for record, _, result in _SERIES],
    ids=_SERIES_IDS,
)
def test_record_emits_exactly_one_emf_line(
    capsys: pytest.CaptureFixture[str],
    record: Callable[[Any], None],
    result: str,
) -> None:
    """呼び出し 1 回につき processing_outcome の EMF 行がちょうど 1 行出る。"""
    record(result)

    assert len(metric_records(capsys.readouterr().out, _EMF_METRIC)) == 1


@pytest.mark.parametrize(("record", "stage", "result"), _SERIES, ids=_SERIES_IDS)
def test_emf_record_carries_stage_and_result_as_called(
    capsys: pytest.CaptureFixture[str],
    record: Callable[[Any], None],
    stage: str,
    result: str,
) -> None:
    """EMF record の stage / result dimension と value が呼び出し引数通り。"""
    record(result)

    rec = metric_records(capsys.readouterr().out, _EMF_METRIC)[0]
    assert rec["stage"] == stage
    assert rec["result"] == result
    assert rec[_EMF_METRIC] == 1
