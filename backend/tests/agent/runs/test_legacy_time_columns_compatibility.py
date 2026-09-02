"""run時刻列の物理削除前後で同じライフサイクルを保証する。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import AnswerPlanSummary, AnswerQuestionResult
from app.agent.runs.contracts import CompleteRunOutcome, StartRunOutcome
from app.agent.runs.repository import AgentRunRepository
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from tests.conftest import TEST_USER_ID

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_columns_present", [True, False], ids=["before-drop", "after-drop"]
)
async def test_run_lifecycle_does_not_depend_on_legacy_time_columns(
    session_factory: async_sessionmaker[AsyncSession],
    legacy_columns_present: bool,
) -> None:
    """列の有無によらず回答を確定でき、旧列が残っていても時刻を書き込まない。"""
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    user_id = UUID(TEST_USER_ID)
    async with session_factory() as schema_session:
        connection = await schema_session.connection()
        try:
            # schema差分はテスト専用のrun表に閉じ、DDLを含めて最後にrollbackする。
            await connection.execute(text("CREATE SCHEMA run_time_columns_compat"))
            await connection.execute(
                text(
                    "CREATE TABLE run_time_columns_compat.agent_runs "
                    "(LIKE public.agent_runs INCLUDING ALL)"
                )
            )
            if legacy_columns_present:
                await connection.execute(
                    text(
                        "ALTER TABLE run_time_columns_compat.agent_runs "
                        "ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NULL, "
                        "ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ NULL"
                    )
                )
            else:
                await connection.execute(
                    text(
                        "ALTER TABLE run_time_columns_compat.agent_runs "
                        "DROP COLUMN IF EXISTS started_at, "
                        "DROP COLUMN IF EXISTS completed_at"
                    )
                )
            await connection.execute(
                text("SET LOCAL search_path TO run_time_columns_compat, public")
            )
            sessions = async_sessionmaker(
                connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with sessions.begin() as session:
                created = await AgentRunRepository(session).create_user_run(
                    user_id=user_id,
                    question="時刻列に依存しない質問",
                    thread_id=None,
                    now=now,
                )
            async with sessions.begin() as session:
                started = await AgentRunRepository(session).start_run(
                    created.run_id, now=now + timedelta(seconds=1)
                )
            assert started.start_outcome is StartRunOutcome.STARTED
            assert started.attempt_epoch == 1

            answer = "時刻列に依存しない回答"
            async with sessions.begin() as session:
                completed = await AgentRunRepository(session).complete_run(
                    run_id=created.run_id,
                    expected_attempt_epoch=1,
                    result=AnswerQuestionResult(
                        status="answered",
                        answer=answer,
                        sources=[],
                        missing_aspects=[],
                        plan_summary=AnswerPlanSummary(plan_type="direct_answer"),
                    ),
                    now=now + timedelta(seconds=2),
                )
            assert completed is CompleteRunOutcome.COMPLETED

            async with sessions() as observer:
                response = await AgentRunRepository(observer).read_run_for_user(
                    run_id=created.run_id, user_id=user_id
                )
                content = await observer.scalar(
                    select(AgentMessage.content)
                    .join(AgentRun, AgentRun.assistant_message_id == AgentMessage.id)
                    .where(AgentRun.id == created.run_id)
                )
                assert response is not None
                assert response.status == "completed"
                assert response.attempt_epoch == 1
                assert content == answer
                if legacy_columns_present:
                    persisted = (
                        await observer.execute(
                            text(
                                "SELECT started_at, completed_at "
                                "FROM run_time_columns_compat.agent_runs "
                                "WHERE id = :run_id"
                            ),
                            {"run_id": created.run_id},
                        )
                    ).one()
                    assert persisted.started_at is None
                    assert persisted.completed_at is None
        finally:
            await schema_session.rollback()
