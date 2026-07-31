"""progress_stage 語彙 6 値化 migration の往復契約。

正本仕様: ``specs/agent-progress-stage-vocabulary-slice.md`` の Test contract
(migration)。対象は ``alembic/versions/`` に ``*progress_stage_vocabulary*`` で
glob される新規 migration ファイル (未実装)。
``tests/test_agent_policy_blocked_migration.py`` と同じく、実 ``agent_runs`` を
TEMPORARY TABLE で shadow し、``op`` を素の ``Operations`` へ差し替えて
upgrade/downgrade を直接呼ぶ。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

_VERSIONS_DIR = Path(__file__).parents[1] / "alembic" / "versions"
_CHECK_VIOLATION_SQLSTATE = "23514"


def _load_migration() -> ModuleType:
    paths = sorted(_VERSIONS_DIR.glob("*progress_stage_vocabulary*.py"))
    assert len(paths) == 1, "missing progress_stage_vocabulary migration"
    spec = importlib.util.spec_from_file_location(
        "test_z10_progress_stage_vocabulary",
        paths[0],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _invoke_migration(
    connection: AsyncConnection,
    migration: ModuleType,
    operation: str,
) -> None:
    def invoke(sync_connection: object) -> None:
        context = MigrationContext.configure(sync_connection)  # type: ignore[arg-type]
        migration.op = Operations(context)
        getattr(migration, operation)()

    await connection.run_sync(invoke)


async def _create_legacy_agent_runs_table(connection: AsyncConnection) -> None:
    """migration 前の CHECK 制約を持つ ``agent_runs`` を temp table で shadow する。"""
    await connection.execute(
        text(
            """
            CREATE TEMPORARY TABLE agent_runs (
                id UUID PRIMARY KEY,
                progress_stage VARCHAR(32) NULL,
                CONSTRAINT ck_agent_runs_progress_stage
                    CHECK (progress_stage IN (
                        'planning', 'retrieving', 'synthesizing'
                    ))
            ) ON COMMIT PRESERVE ROWS
            """
        )
    )


async def _create_current_agent_runs_table(connection: AsyncConnection) -> None:
    """migration 後の CHECK 制約を持つ ``agent_runs`` を temp table で shadow する。"""
    await connection.execute(
        text(
            """
            CREATE TEMPORARY TABLE agent_runs (
                id UUID PRIMARY KEY,
                progress_stage VARCHAR(32) NULL,
                CONSTRAINT ck_agent_runs_progress_stage
                    CHECK (progress_stage IN (
                        'safety_check', 'context_resolution', 'planning',
                        'evidence_collection', 'evidence_review', 'answering'
                    ))
            ) ON COMMIT PRESERVE ROWS
            """
        )
    )


async def _insert_progress_stage(
    connection: AsyncConnection, *, run_id: str, stage: str | None
) -> None:
    await connection.execute(
        text("INSERT INTO agent_runs (id, progress_stage) VALUES (:id, :stage)"),
        {"id": run_id, "stage": stage},
    )


async def _read_progress_stage(
    connection: AsyncConnection, *, run_id: str
) -> str | None:
    return await connection.scalar(
        text("SELECT progress_stage FROM agent_runs WHERE id = :id"),
        {"id": run_id},
    )


async def _assert_progress_stage_rejected(
    connection: AsyncConnection, *, stage: str
) -> None:
    """``stage`` の INSERT が CHECK 制約違反 (sqlstate 23514) で拒否されることを

    確認する。
    """
    with pytest.raises(IntegrityError) as exc_info:
        async with connection.begin_nested():
            await _insert_progress_stage(connection, run_id=str(uuid4()), stage=stage)
    orig = exc_info.value.orig
    cause = getattr(orig, "__cause__", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(cause, "sqlstate", None)
    assert sqlstate == _CHECK_VIOLATION_SQLSTATE


@pytest.mark.asyncio
async def test_upgrade_translates_existing_rows_to_new_vocabulary(
    db_session: AsyncSession,
) -> None:
    """retrievingはevidence_collectionへ、synthesizingはansweringへ写り、

    planningとNULLは変わらない。
    """
    migration = _load_migration()
    connection = await db_session.connection()
    await _create_legacy_agent_runs_table(connection)
    planning_id = str(uuid4())
    retrieving_id = str(uuid4())
    synthesizing_id = str(uuid4())
    null_id = str(uuid4())
    await _insert_progress_stage(connection, run_id=planning_id, stage="planning")
    await _insert_progress_stage(connection, run_id=retrieving_id, stage="retrieving")
    await _insert_progress_stage(
        connection, run_id=synthesizing_id, stage="synthesizing"
    )
    await _insert_progress_stage(connection, run_id=null_id, stage=None)

    await _invoke_migration(connection, migration, "upgrade")

    assert await _read_progress_stage(connection, run_id=planning_id) == "planning"
    assert (
        await _read_progress_stage(connection, run_id=retrieving_id)
        == "evidence_collection"
    )
    assert await _read_progress_stage(connection, run_id=synthesizing_id) == "answering"
    assert await _read_progress_stage(connection, run_id=null_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "safety_check",
        "context_resolution",
        "planning",
        "evidence_collection",
        "evidence_review",
        "answering",
    ],
)
async def test_upgrade_accepts_all_six_new_stage_values(
    db_session: AsyncSession, stage: str
) -> None:
    """upgrade後、6値すべてがCHECK制約に拒否されずINSERTできる。"""
    migration = _load_migration()
    connection = await db_session.connection()
    await _create_legacy_agent_runs_table(connection)
    await _invoke_migration(connection, migration, "upgrade")

    run_id = str(uuid4())
    await _insert_progress_stage(connection, run_id=run_id, stage=stage)

    assert await _read_progress_stage(connection, run_id=run_id) == stage


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["retrieving", "synthesizing"])
async def test_upgrade_rejects_legacy_stage_values(
    db_session: AsyncSession, stage: str
) -> None:
    """upgrade後、旧語彙retrieving/synthesizingがCHECK制約に弾かれる。"""
    migration = _load_migration()
    connection = await db_session.connection()
    await _create_legacy_agent_runs_table(connection)
    await _invoke_migration(connection, migration, "upgrade")

    await _assert_progress_stage_rejected(connection, stage=stage)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_stage", "expected_legacy_stage"),
    [
        pytest.param("evidence_collection", "retrieving", id="evidence_collection"),
        pytest.param("evidence_review", "retrieving", id="evidence_review"),
        pytest.param("answering", "synthesizing", id="answering"),
        pytest.param("safety_check", None, id="safety_check"),
        pytest.param("context_resolution", None, id="context_resolution"),
        pytest.param("planning", "planning", id="planning"),
    ],
)
async def test_downgrade_translates_new_rows_to_legacy_vocabulary(
    db_session: AsyncSession,
    new_stage: str,
    expected_legacy_stage: str | None,
) -> None:
    """downgradeでevidence_collection/evidence_reviewはretrievingへ、

    answeringはsynthesizingへ戻り、safety_check/context_resolutionはNULLへ落ちる。
    planningは不変。
    """
    migration = _load_migration()
    connection = await db_session.connection()
    await _create_current_agent_runs_table(connection)
    run_id = str(uuid4())
    await _insert_progress_stage(connection, run_id=run_id, stage=new_stage)

    await _invoke_migration(connection, migration, "downgrade")

    assert (
        await _read_progress_stage(connection, run_id=run_id) == expected_legacy_stage
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "safety_check",
        "context_resolution",
        "evidence_collection",
        "evidence_review",
        "answering",
    ],
)
async def test_downgrade_rejects_new_stage_values(
    db_session: AsyncSession, stage: str
) -> None:
    """downgrade後、新語彙がCHECK制約に弾かれる。"""
    migration = _load_migration()
    connection = await db_session.connection()
    await _create_current_agent_runs_table(connection)
    await _invoke_migration(connection, migration, "downgrade")

    await _assert_progress_stage_rejected(connection, stage=stage)
