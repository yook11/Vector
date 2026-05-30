"""監査入力に prompt-injection 境界タグを検出した事実の Logfire metric。

監査 payload に焼く外部入力 (completion の body_head / curation の input_content)
に ``<untrusted_input>`` 境界タグが混じっていた回数を stage 別 counter で記録する。
個別調査用の structured log は検知 site 側で emit し、本 module は「率」の集計に
純化する (`app/analysis/rate_limit/metrics.py` と同方針)。attributes は cardinality
爆発を避けるため低 cardinality (stage) のみとし、article_id / URL は載せない。
"""

from __future__ import annotations

import logfire

_injection_boundary_counter = logfire.metric_counter(
    "vector.audit.injection_boundary_detected",
    unit="1",
    description="監査入力に untrusted 境界タグ injection を検出した回数 (stage 別)",
)


def record_injection_boundary_detected(*, stage: str) -> None:
    """境界タグ検知を metric counter に 1 件記録する。"""
    _injection_boundary_counter.add(1, attributes={"stage": stage})
