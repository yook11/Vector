"""Thread contracts shared with agent application services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ThreadMessageSnapshot:
    """Question context preparation に渡すthread内メッセージの最小投影。"""

    role: Literal["user", "assistant"]
    content: str
    missing_aspects: tuple[str, ...] = ()
