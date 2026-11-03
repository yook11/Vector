"""worker が実行する run の質問を読む。"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread


@dataclass(frozen=True, slots=True)
class UserQuestionMessage:
    content: str
    seq: int


class AgentRunQuestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read_user_question_for_run(
        self,
        run_id: uuid_mod.UUID,
    ) -> tuple[uuid_mod.UUID, uuid_mod.UUID, UserQuestionMessage] | None:
        row = (
            await self._session.execute(
                select(
                    AgentThread.user_id,
                    AgentRun.thread_id,
                    AgentMessage.content,
                    AgentMessage.seq,
                )
                .join(AgentMessage, AgentRun.user_message_id == AgentMessage.id)
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(AgentRun.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        user_id, thread_id, content, seq = row
        return user_id, thread_id, UserQuestionMessage(content=content, seq=seq)
