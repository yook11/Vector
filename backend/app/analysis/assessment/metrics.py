"""新Assessmentの保存成功・処理失敗を低cardinalityの結果別に集計する。"""

from __future__ import annotations

from typing import Literal

import logfire

from app.cloudwatch.emf import emit_metric

# 成功率の分母は in_scope+out_of_scope+failed。
AssessmentProcessingOutcome = Literal["in_scope", "out_of_scope", "failed"]

_processing_outcome_counter = logfire.metric_counter(
    "vector.assessment.processing_outcome",
    unit="1",
    description=(
        "assessment 処理試行の結末件数。result 別 (in_scope/out_of_scope/failed)"
    ),
)


def record_assessment_processing_outcome(result: AssessmentProcessingOutcome) -> None:
    """assessment 処理試行の結末を counter に 1 件記録する。"""
    _processing_outcome_counter.add(1, attributes={"result": result})
    # CloudWatch 失敗率 alarm が消費する二重 sink。Terraform 側と契約名を揃える。
    emit_metric(
        "processing_outcome",
        dimensions={"stage": "assessment", "result": result},
        value=1,
        unit="Count",
    )
