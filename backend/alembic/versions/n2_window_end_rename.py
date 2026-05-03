"""rename_week_start_to_window_end

`weekly_trends_snapshots.week_start` (JST 月曜) を `window_end` (rolling 7d
window の上限となる任意の JST 日付) に rename する。意味は「先週月曜」から
「半開区間 [window_end - 7d, window_end) の上端」へ転換し、cron は週次から
毎日 (JST 00:05) に切り替わる。

PK 制約は PostgreSQL が column rename に追従するため触らない。DESC index は
名前と参照列の両方を貼り直す。

Revision ID: n2_window_end_rename
Revises: n1_deactivate_red_sources
Create Date: 2026-05-03 00:00:00.000000

"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "n2_window_end_rename"
down_revision: str | None = "n1_deactivate_red_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    op.drop_index(
        "ix_weekly_trends_snapshots_week_start_desc",
        table_name="weekly_trends_snapshots",
    )
    op.alter_column(
        "weekly_trends_snapshots",
        "week_start",
        new_column_name="window_end",
    )
    op.create_index(
        "ix_weekly_trends_snapshots_window_end_desc",
        "weekly_trends_snapshots",
        [text("window_end DESC")],
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    op.drop_index(
        "ix_weekly_trends_snapshots_window_end_desc",
        table_name="weekly_trends_snapshots",
    )
    op.alter_column(
        "weekly_trends_snapshots",
        "window_end",
        new_column_name="week_start",
    )
    op.create_index(
        "ix_weekly_trends_snapshots_week_start_desc",
        "weekly_trends_snapshots",
        [text("week_start DESC")],
    )
