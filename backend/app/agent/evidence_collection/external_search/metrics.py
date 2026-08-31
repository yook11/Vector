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


_external_hit_truncated_counter = logfire.metric_counter(
    "vector.agent.external_search.hit_truncated",
    unit="1",
    description="External search provider hits whose body was truncated at intake",
)


def record_external_hit_truncated() -> None:
    """本文をEXTERNAL_CONTENT_MAX_CHARSで切り詰めた回数。

    切り詰め幅は本番のトークン実測で決めるため、効いた頻度が見えないと判断できない。
    """

    _external_hit_truncated_counter.add(1)
