"""External search hit intake metrics."""

from __future__ import annotations

from typing import Literal

import logfire

ExternalHitDropReason = Literal[
    "result_not_mapping",
    "title_missing",
    "url_unsafe",
    "content_too_long",
]

_external_hit_dropped_counter = logfire.metric_counter(
    "vector.agent.external_search.hit_dropped",
    unit="1",
    description="External search provider hits dropped at intake, by reason",
)


def record_external_hit_dropped(*, reason: ExternalHitDropReason) -> None:
    """provider応答1件を落とした理由を記録する。URL・本文・queryは載せない。"""

    _external_hit_dropped_counter.add(1, attributes={"reason": reason})
