"""古い形のトレンドのスナップショットを削除するmigrationが、他の出力を残すことを確認する。"""

import pytest

from alembic import command
from local_tests.migrations.support import migrate

REVISION = "z34_delete_trends_snapshots"
PREDECESSOR = "z33_lock_exclusion_parents"


@pytest.fixture
async def predecessor_database(system_database):
    await migrate(system_database, command.downgrade, PREDECESSOR)
    return system_database


async def _insights_rows(connection):
    return (
        await connection.fetch("SELECT * FROM trends_snapshots ORDER BY window_end"),
        await connection.fetch("SELECT * FROM weekly_briefings ORDER BY id"),
    )


async def test_deletes_all_trends_snapshots_and_keeps_briefings(predecessor_database):
    """古い形のトレンドを全件削除し、ブリーフィングは残す。"""
    database = predecessor_database
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO trends_snapshots "
            "(window_end, bundle, source_analysis_count, generated_at) VALUES "
            "('2026-10-05', '{\"windowEnd\": \"2026-10-05\"}'::jsonb, 5, "
            "'2026-10-05T00:05:00+09:00'), "
            "('2026-10-06', '{\"windowEnd\": \"2026-10-06\"}'::jsonb, 7, "
            "'2026-10-06T00:05:00+09:00')"
        )
        await connection.execute(
            "INSERT INTO weekly_briefings "
            "(week_start_date, category_id, headline, summary, chapters, "
            "key_articles, watch_points, model_name, input_article_count) "
            "SELECT '2026-09-28', id, '既存の見出し', '既存の要約', '[]'::jsonb, "
            "'[]'::jsonb, '[]'::jsonb, 'existing-model', 3 "
            "FROM categories ORDER BY id LIMIT 1"
        )
        snapshots, briefings = await _insights_rows(connection)
    assert (len(snapshots), len(briefings)) == (2, 1)

    await migrate(database, command.upgrade, REVISION)

    async with database.connect("vector") as connection:
        assert await _insights_rows(connection) == ([], briefings)
