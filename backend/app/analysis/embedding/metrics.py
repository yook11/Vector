"""embedding 処理試行の結末を集計する Logfire metric。

インフラ障害 (infra_error) を成功率の分母から外して可視化するための counter。
span helper の影ではなく、分類が判明する service / task / handler 境界で emit する。
attributes は低 cardinality の result のみとし、analyzed_article_id 等の ID は載せない。
"""

from __future__ import annotations

from typing import Literal

import logfire

# 成功率の分母は succeeded+failed。infra_error は emit するが分母外。
EmbeddingProcessingOutcome = Literal["succeeded", "failed", "infra_error"]

_processing_outcome_counter = logfire.metric_counter(
    "vector.embedding.processing_outcome",
    unit="1",
    description=(
        "embedding 処理試行の結末件数。result 別 (succeeded/failed/infra_error)"
    ),
)


def record_embedding_processing_outcome(result: EmbeddingProcessingOutcome) -> None:
    """embedding 処理試行の結末を counter に 1 件記録する。"""
    _processing_outcome_counter.add(1, attributes={"result": result})
