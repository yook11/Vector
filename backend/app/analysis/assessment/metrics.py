"""assessment 処理試行の結末を集計する Logfire metric。

インフラ障害 (infra_error) を成功率の分母から外して可視化するための counter。
span helper の影ではなく、分類が判明する task / service / handler 境界で emit する。
attributes は低 cardinality の result のみとし、curation_id 等の ID は載せない。
"""

from __future__ import annotations

from typing import Literal

import logfire

# 成功率の分母は in_scope+out_of_scope+failed。infra_error は emit するが分母外。
AssessmentProcessingOutcome = Literal[
    "in_scope", "out_of_scope", "failed", "infra_error"
]

_processing_outcome_counter = logfire.metric_counter(
    "vector.assessment.processing_outcome",
    unit="1",
    description=(
        "assessment 処理試行の結末件数。result 別 "
        "(in_scope/out_of_scope/failed/infra_error)"
    ),
)


def record_assessment_processing_outcome(result: AssessmentProcessingOutcome) -> None:
    """assessment 処理試行の結末を counter に 1 件記録する。"""
    _processing_outcome_counter.add(1, attributes={"result": result})
