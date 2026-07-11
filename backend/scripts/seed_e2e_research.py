"""Research pending navigation E2E用の固定threadを投入・削除する。"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, insert, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection  # noqa: E402

from app.config import settings  # noqa: E402
from app.db_ssl import create_app_engine  # noqa: E402
from app.models.agent_message import AgentMessage  # noqa: E402
from app.models.agent_run import AgentRun  # noqa: E402
from app.models.agent_thread import AgentThread  # noqa: E402
from app.models.auth_ref import auth_user_ref  # noqa: E402

_E2E_USER_ID = uuid.UUID("01900000-0000-7000-a000-00000000e2e1")


@dataclass(frozen=True)
class FixtureThread:
    label: str
    thread_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    run_id: uuid.UUID
    title: str
    question: str
    answer: str
    updated_at: dt.datetime


FIXTURE_THREADS = (
    FixtureThread(
        label="A",
        thread_id=uuid.UUID("00000000-0000-4000-a000-00000000e2a1"),
        user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000a101"),
        assistant_message_id=uuid.UUID("00000000-0000-4000-a000-00000000a1a1"),
        run_id=uuid.UUID("00000000-0000-4000-a000-00000000a1f1"),
        title="E2E Research Alpha",
        question="Alpha market question",
        answer="Alpha answer marker",
        updated_at=dt.datetime(2026, 7, 11, 3, 0, tzinfo=dt.UTC),
    ),
    FixtureThread(
        label="B",
        thread_id=uuid.UUID("00000000-0000-4000-a000-00000000e2b2"),
        user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000b201"),
        assistant_message_id=uuid.UUID("00000000-0000-4000-a000-00000000b2a1"),
        run_id=uuid.UUID("00000000-0000-4000-a000-00000000b2f1"),
        title="E2E Research Beta",
        question="Beta market question",
        answer="Beta answer marker",
        updated_at=dt.datetime(2026, 7, 11, 2, 0, tzinfo=dt.UTC),
    ),
    FixtureThread(
        label="C",
        thread_id=uuid.UUID("00000000-0000-4000-a000-00000000e2c3"),
        user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000c301"),
        assistant_message_id=uuid.UUID("00000000-0000-4000-a000-00000000c3a1"),
        run_id=uuid.UUID("00000000-0000-4000-a000-00000000c3f1"),
        title="E2E Research Gamma",
        question="Gamma market question",
        answer="Gamma answer marker",
        updated_at=dt.datetime(2026, 7, 11, 1, 0, tzinfo=dt.UTC),
    ),
)

_THREAD_IDS = tuple(thread.thread_id for thread in FIXTURE_THREADS)


def guard_production(environment: str) -> None:
    if environment.lower() != "production":
        return
    print(
        "ERROR: seed_e2e_research.py must NOT run in production.",
        file=sys.stderr,
    )
    raise SystemExit(2)


async def _cleanup(connection: AsyncConnection) -> None:
    await connection.execute(delete(AgentThread).where(AgentThread.id.in_(_THREAD_IDS)))


async def _seed(connection: AsyncConnection) -> None:
    owner = (
        await connection.execute(
            select(auth_user_ref.c.id).where(auth_user_ref.c.id == _E2E_USER_ID)
        )
    ).scalar_one_or_none()
    if owner is None:
        raise RuntimeError("E2E user is missing; run scripts/seed_e2e_users.py first")

    await _cleanup(connection)
    await connection.execute(
        insert(AgentThread),
        [
            {
                "id": thread.thread_id,
                "user_id": _E2E_USER_ID,
                "title": thread.title,
                "created_at": thread.updated_at,
                "updated_at": thread.updated_at,
            }
            for thread in FIXTURE_THREADS
        ],
    )
    await connection.execute(
        insert(AgentMessage),
        [
            row
            for thread in FIXTURE_THREADS
            for row in (
                {
                    "id": thread.user_message_id,
                    "thread_id": thread.thread_id,
                    "seq": 1,
                    "role": "user",
                    "content": thread.question,
                    "missing_aspects": [],
                    "created_at": thread.updated_at,
                },
                {
                    "id": thread.assistant_message_id,
                    "thread_id": thread.thread_id,
                    "seq": 2,
                    "role": "assistant",
                    "content": thread.answer,
                    "missing_aspects": [],
                    "created_at": thread.updated_at,
                },
            )
        ],
    )
    await connection.execute(
        insert(AgentRun),
        [
            {
                "id": thread.run_id,
                "thread_id": thread.thread_id,
                "user_message_id": thread.user_message_id,
                "assistant_message_id": thread.assistant_message_id,
                "status": "completed",
                "progress_stage": "synthesizing",
                "error_code": None,
                "created_at": thread.updated_at,
                "started_at": thread.updated_at,
                "completed_at": thread.updated_at,
            }
            for thread in FIXTURE_THREADS
        ],
    )


async def run(command: str) -> None:
    database_url = settings.migration_database_url or settings.database_url
    engine = create_app_engine(
        database_url,
        application_name="vector-cli-seed-e2e-research",
    )
    try:
        async with engine.begin() as connection:
            if command == "seed":
                await _seed(connection)
            else:
                await _cleanup(connection)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seed", "cleanup"))
    args = parser.parse_args()
    guard_production(os.environ.get("ENV", ""))
    asyncio.run(run(args.command))


if __name__ == "__main__":
    main()
