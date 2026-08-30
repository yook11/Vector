"""記録の共通語彙。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "PhaseCall",
    "PhaseStatus",
    "ToolCall",
    "Usage",
]


class PhaseStatus(StrEnum):
    """作業単位の終わり方。工程の結論語彙ではない。"""

    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class Usage:
    """provider が返した token 数。欠損は None のままにし、0 で埋めない。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None


def _optional_token(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _usage_from_optional_counts(
    *,
    input_tokens: object = None,
    output_tokens: object = None,
    cache_read_input_tokens: object = None,
    reasoning_output_tokens: object = None,
) -> Usage | None:
    """整数以外は欠損とし、4欄すべて欠損なら Usage を作らない。"""

    present = Usage(
        input_tokens=_optional_token(input_tokens),
        output_tokens=_optional_token(output_tokens),
        cache_read_input_tokens=_optional_token(cache_read_input_tokens),
        reasoning_output_tokens=_optional_token(reasoning_output_tokens),
    )
    if (
        present.input_tokens is None
        and present.output_tokens is None
        and present.cache_read_input_tokens is None
        and present.reasoning_output_tokens is None
    ):
        return None
    return present


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_name: str
    started_at: float


@dataclass(frozen=True, slots=True)
class PhaseCall:
    started_at: float
