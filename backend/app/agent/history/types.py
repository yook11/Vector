"""Agent run state vocabulary."""

from __future__ import annotations

from enum import StrEnum


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunErrorCode(StrEnum):
    GENERATION_UNAVAILABLE = "generation_unavailable"
    INTERNAL_ERROR = "internal_error"
    ENQUEUE_FAILED = "enqueue_failed"
    STALE = "stale"
