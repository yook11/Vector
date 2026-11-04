"""Run の実行を worker へ依頼する引数と、broker の .kiq() を隠す enqueuer。"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request
from pydantic import BaseModel, ConfigDict


class AgentRunTrigger(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID


class AgentRunEnqueuer:
    async def enqueue(self, run_id: UUID) -> None:
        from app.queue.tasks.agent_run import run_agent_answer

        await run_agent_answer.kiq(AgentRunTrigger(run_id=run_id))


def get_agent_run_enqueuer(request: Request) -> AgentRunEnqueuer:
    return request.app.state.agent_run_enqueuer
