"""add news_sources.attribution_label

Phase 3 prep: 法令上「出典の明示」が必要な Tier 1 ソース (CC BY 4.0 / 政府
標準利用規約 等) の表示用文言を保持するカラムを追加する。

frontend は ``source.attribution_label ?? source.name`` の単純 fallback で
表示する想定 (UI 反映は別 PR)。既存ソース行は NULL のまま残し、Phase 3 各
PR の bulk_insert で新規 Tier 1 ソースに正しい値を埋める。

ALTER TABLE ADD COLUMN は nullable + default なしのため Postgres では
metadata-only の高速 DDL となる。lock_timeout は念のため明示。

Revision ID: o1_attribution_label
Revises: n3_grant_app_db_users
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o1_attribution_label"
down_revision: str | None = "n3_grant_app_db_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.add_column(
        "news_sources",
        sa.Column("attribution_label", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_column("news_sources", "attribution_label")
