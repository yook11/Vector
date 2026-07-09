"""Agent run task trigger DTO."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AgentRunTrigger(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
