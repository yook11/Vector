"""curation 処理試行の結末を集計する Logfire metric。

Consumerの失敗は原因によらずfailedとして数える。
span helper の影ではなく、分類が判明する task / service / handler 境界で emit する。
attributes は低 cardinality の result のみとし、article_id 等の ID は載せない。
"""

from __future__ import annotations

from typing import Literal

import logfire

from app.cloudwatch.emf import emit_metric

# 成功率の分母は signal+noise+rejected+failed。
CurationProcessingOutcome = Literal["signal", "noise", "rejected", "failed"]

_processing_outcome_counter = logfire.metric_counter(
    "vector.curation.processing_outcome",
    unit="1",
    description=(
        "curation 処理試行の結末件数。result 別 (signal/noise/rejected/failed)"
    ),
)


def record_curation_processing_outcome(result: CurationProcessingOutcome) -> None:
    """curation 処理試行の結末を counter に 1 件記録する。"""
    _processing_outcome_counter.add(1, attributes={"result": result})
    # CloudWatch 失敗率 alarm が消費する二重 sink。Terraform 側と契約名を揃える。
    emit_metric(
        "processing_outcome",
        dimensions={"stage": "curation", "result": result},
        value=1,
        unit="Count",
    )
