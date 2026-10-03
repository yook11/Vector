"""排他トリガーの親ロックmigrationの往復で、ロックと権限が一緒に入れ替わることを確認する。"""

import pytest

from alembic import command
from local_tests.migrations.support import migrate
from local_tests.permissions.support import read_column_permissions

REVISION = "z33_lock_exclusion_parents"
PREDECESSOR = "z32_normalize_web_urls"
ROLE = "vector_article_analysis"
FUNCTIONS = (
    "enforce_no_curation_noise_for_curation",
    "enforce_no_curation_for_curation_noise",
    "enforce_no_out_of_scope_article_for_analyzed_article",
    "enforce_no_analyzed_article_for_out_of_scope_article",
)
LOCK_GRANTS = {
    ("public", "analyzable_articles", "UPDATE"): {"id"},
    ("public", "article_curations", "UPDATE"): {"id"},
}


async def _observe(database):
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT proname, prosrc LIKE '%FOR NO KEY UPDATE%' AS locks_parent "
            "FROM pg_proc WHERE proname = ANY($1::text[])",
            list(FUNCTIONS),
        )
    async with database.connect(ROLE) as connection:
        columns = await read_column_permissions(connection)
    locking = {row["proname"]: row["locks_parent"] for row in rows}
    lock_grants = {key: value for key, value in columns.items() if key in LOCK_GRANTS}
    return locking, lock_grants


@pytest.mark.asyncio
async def test_exclusion_lock_round_trip(system_database):
    """downgradeで4つの関数が親をロックしなくなり権限も外れ、再upgradeで両方戻る。"""
    locked = ({name: True for name in FUNCTIONS}, LOCK_GRANTS)
    assert await _observe(system_database) == locked

    await migrate(system_database, command.downgrade, PREDECESSOR)
    assert await _observe(system_database) == ({name: False for name in FUNCTIONS}, {})

    await migrate(system_database, command.upgrade, REVISION)
    assert await _observe(system_database) == locked
