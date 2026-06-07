"""rename weekly_trends_snapshots to trends_snapshots

Trend Discovery の集計は rolling 7d を日次再生成するもので weekly ではない。
table / index / constraint から "weekly" 語彙を外し、BC 全体 (domain VO・schema・
endpoint) の de-Weekly 改名に DB 物理名を揃える。

rename のみでデータは保持される (行は移送される)。``op.rename_table`` は
index / check / primary key を移送するが名前は変えないため、index・check・PK
constraint も明示的に rename し、``Base.metadata`` から生成される命名 (新表名規約)
とのドリフトを防ぐ。

Revision ID: r4_rename_trends_snapshots
Revises: w1_briefings_key_articles
Create Date: 2026-06-07 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "r4_rename_trends_snapshots"
down_revision: str | None = "w1_briefings_key_articles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: op.rename_table + ALTER 系 op.execute を含む table rename のため contract。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    op.rename_table("weekly_trends_snapshots", "trends_snapshots")
    op.execute(
        "ALTER INDEX ix_weekly_trends_snapshots_window_end_desc "
        "RENAME TO ix_trends_snapshots_window_end_desc"
    )
    op.execute(
        "ALTER TABLE trends_snapshots "
        "RENAME CONSTRAINT ck_weekly_trends_snapshots_count_non_negative "
        "TO ck_trends_snapshots_count_non_negative"
    )
    op.execute(
        "ALTER TABLE trends_snapshots "
        "RENAME CONSTRAINT weekly_trends_snapshots_pkey "
        "TO trends_snapshots_pkey"
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    op.execute(
        "ALTER TABLE trends_snapshots "
        "RENAME CONSTRAINT trends_snapshots_pkey "
        "TO weekly_trends_snapshots_pkey"
    )
    op.execute(
        "ALTER TABLE trends_snapshots "
        "RENAME CONSTRAINT ck_trends_snapshots_count_non_negative "
        "TO ck_weekly_trends_snapshots_count_non_negative"
    )
    op.execute(
        "ALTER INDEX ix_trends_snapshots_window_end_desc "
        "RENAME TO ix_weekly_trends_snapshots_window_end_desc"
    )
    op.rename_table("trends_snapshots", "weekly_trends_snapshots")
