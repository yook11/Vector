"""add weekly_trends_snapshots table

週次トレンドの 1 週間分まとまりを 1 行 1 JSONB として保存するテーブルを追加する。

設計:
- ``week_start`` (JST 月曜 00:00 起点の date) を主キーにし、週ごとに 1 行を保証
- ``bundle`` は ``WeeklyTrendsBundle.model_dump(mode="json")`` 出力をそのまま格納
  (snapshot は 1 単位保存が責務であり、推移分析や横断クエリのために
  正規化テーブル群に分解しない: feedback_snapshot_responsibility.md)
- ``generated_at`` は監査用、``source_analysis_count`` は集計件数の可観測性
- ``source_analysis_count >= 0`` を CHECK で構造的に強制
- ``ix_weekly_trends_snapshots_week_start_desc`` は
  「直近 snapshot を取得」クエリ (find_latest) の高速化用 (DESC index)

Revision ID: 109044f13a69
Revises: b7eadad7f3cc
Create Date: 2026-04-27 11:42:06.094709

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "109044f13a69"
down_revision: str | None = "b7eadad7f3cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weekly_trends_snapshots",
        sa.Column("week_start", sa.Date(), primary_key=True),
        sa.Column(
            "bundle",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("source_analysis_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "source_analysis_count >= 0",
            name="ck_weekly_trends_snapshots_count_non_negative",
        ),
    )
    op.create_index(
        "ix_weekly_trends_snapshots_week_start_desc",
        "weekly_trends_snapshots",
        [sa.text("week_start DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_weekly_trends_snapshots_week_start_desc",
        table_name="weekly_trends_snapshots",
    )
    op.drop_table("weekly_trends_snapshots")
