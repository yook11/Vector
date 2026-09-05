"""Agent run lifecycle contracts and outcomes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserQuestionMessage:
    content: str
    seq: int
