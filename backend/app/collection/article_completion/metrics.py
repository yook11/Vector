"""completion 処理試行の結末を集計する Logfire metric。"""

from __future__ import annotations

from typing import Literal

import logfire

# 成功率の分母は succeeded+failed。infra_error は emit するが分母外。
CompletionProcessingOutcome = Literal["succeeded", "failed", "infra_error"]

_processing_outcome_counter = logfire.metric_counter(
    "vector.completion.processing_outcome",
    unit="1",
    description=(
        "completion 処理試行の結末件数。result 別 (succeeded/failed/infra_error)"
    ),
)


def record_completion_processing_outcome(result: CompletionProcessingOutcome) -> None:
    """completion 処理試行の結末を counter に 1 件記録する。"""
    _processing_outcome_counter.add(1, attributes={"result": result})
