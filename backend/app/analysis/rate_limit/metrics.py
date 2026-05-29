"""rate limit gate skip の Logfire metric。

provider quota を先回りして AI 呼び出しを skip した回数を stage/model 別に counter で
記録する。個別調査用の structured log は各 task 側で emit し、本 module は「率」の集計に
純化する。attributes は cardinality 爆発を避けるため低 cardinality (stage / model)
のみとし、article_id などの ID は載せない。
"""

from __future__ import annotations

import logfire

_rate_limit_gate_skipped_counter = logfire.metric_counter(
    "vector.analysis.rate_limit_gate_skipped",
    unit="1",
    description="rate limit gate が AI call を skip した回数 (stage/model 別)",
)


def record_rate_limit_gate_skipped(*, stage: str, model: str) -> None:
    """rate limit gate skip を metric counter に 1 件記録する。"""
    _rate_limit_gate_skipped_counter.add(1, attributes={"stage": stage, "model": model})
