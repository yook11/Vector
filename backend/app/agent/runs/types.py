"""Agent run state vocabulary."""

from __future__ import annotations

from enum import StrEnum


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    POLICY_BLOCKED = "policy_blocked"
    FAILED = "failed"


class AgentRunErrorCode(StrEnum):
    GENERATION_UNAVAILABLE = "generation_unavailable"
    INTERNAL_ERROR = "internal_error"
    ENQUEUE_FAILED = "enqueue_failed"
    STALE = "stale"
    CANCELLED = "cancelled"


class AgentRunProgressStage(StrEnum):
    SAFETY_CHECK = "safety_check"
    CONTEXT_RESOLUTION = "context_resolution"
    PLANNING = "planning"
    EVIDENCE_COLLECTION = "evidence_collection"
    EVIDENCE_REVIEW = "evidence_review"
    ANSWERING = "answering"
