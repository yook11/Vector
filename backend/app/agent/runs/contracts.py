"""Agent run lifecycle contracts and outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class ThreadNotFoundError(Exception):
    """Requested thread is missing or not owned by the current user."""


class ActiveRunConflictError(Exception):
    """A queued/running run already exists for the requested thread."""


class RunTransitionLostError(Exception):
    """Another actor moved the run before this transition could commit."""


class CancelRunOutcome(StrEnum):
    CANCELLED = "cancelled"
    ALREADY_FAILED = "already_failed"
    ALREADY_COMPLETED = "already_completed"


@dataclass(frozen=True, slots=True)
class CreatedAgentRun:
    thread_id: UUID
    run_id: UUID


@dataclass(frozen=True, slots=True)
class PreparedAgentRun:
    run_id: UUID
    thread_id: UUID
    question: str
    user_message_seq: int
    attempt_epoch: int
