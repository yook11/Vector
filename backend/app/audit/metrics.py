"""監査行書き込み失敗 (pipeline_events append 失敗) の Logfire metric。

各工程の best-effort 監査 (``*_audit_dropped`` ログを残して握り潰す経路) で、書き込み
自体が失敗した回数を stage 別 counter で記録する。個別調査用の structured log は各 drop
site 側に残し、本 module は「率」の集計に純化する (``injection_signal.py`` と同方針)。
attributes は cardinality 爆発を避け stage のみとし、article_id / URL は載せない (I3)。
"""

from __future__ import annotations

import logfire

from app.audit.domain.event import Stage

_audit_dropped_counter = logfire.metric_counter(
    "vector.audit.dropped",
    unit="1",
    description="監査行書き込み失敗を drop した件数 (stage 別)",
)


def record_audit_dropped(stage: Stage) -> None:
    """監査書き込み失敗を 1 件記録する。label は stage のみ (I3)、ID は載せない。"""
    _audit_dropped_counter.add(1, attributes={"stage": stage.value})
