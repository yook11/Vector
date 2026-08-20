"""記録の共通語彙と start ハンドル。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "LlmCall",
    "LlmCallResult",
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


class LlmCallResult(StrEnum):
    """1 provider attempt の結論。span の result 文字列と一致させる。"""

    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class Usage:
    """provider が返した token 数。欠損は None のままにし、0 で埋めない。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LlmCall:
    agent_name: str
    provider: str
    model: str
    attempt_number: int
    started_at: float


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_name: str
    started_at: float


@dataclass(frozen=True, slots=True)
class PhaseCall:
    started_at: float
