"""所有スレッドの詳細取得と、そのスレッドに限定した期限回収。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.running.state_service import AgentRunStateService
from app.agent.threads.repository import AgentThreadRepository
from app.schemas.research import ResearchThreadDetail


async def read_owned_thread_detail(
    session: AsyncSession,
    *,
    thread_id: UUID,
    user_id: UUID,
    state_service: AgentRunStateService,
) -> ResearchThreadDetail | None:
    # 期限を過ぎたRunを先に閉じ、閉じた後の状態を詳細として返す。
    await state_service.end_runs_past_deadline(thread_id=thread_id, user_id=user_id)
    return await AgentThreadRepository(session).read_thread_detail_for_user(
        thread_id=thread_id,
        user_id=user_id,
    )
