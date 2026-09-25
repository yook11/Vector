"""completion 処理試行の結末を集計する Logfire metric。"""

from __future__ import annotations

from typing import Literal

import logfire

from app.cloudwatch.emf import emit_metric

# 失敗は原因によらず failed とし、成功率の分母は succeeded+failed。
CompletionProcessingOutcome = Literal["succeeded", "failed"]

_processing_outcome_counter = logfire.metric_counter(
    "vector.completion.processing_outcome",
    unit="1",
    description="completion 処理試行の結末件数。result 別 (succeeded/failed)",
)


def record_completion_processing_outcome(result: CompletionProcessingOutcome) -> None:
    """completion 処理試行の結末を counter に 1 件記録する。"""
    _processing_outcome_counter.add(1, attributes={"result": result})
    # CloudWatch で成功率を確認する二重 sink (completion は失敗率 alarm を置かない)。
    emit_metric(
        "processing_outcome",
        dimensions={"stage": "completion", "result": result},
        value=1,
        unit="Count",
    )
