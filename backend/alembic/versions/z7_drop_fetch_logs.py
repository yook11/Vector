"""drop fetch_logs table (superseded by pipeline_events).

``fetch_logs`` は acquisition stage の per-source 実行記録テーブル。本番未 deploy
の時点で、pipeline_events 監査基盤 (``SourceAcquisitionAuditRepository.append_*``)
が同 stage の per-article SUCCEEDED + per-failure FAILED を完全に置き換えたため、
読み手ゼロのまま acquisition task が書き続ける死蔵テーブルになっていた。本 migration
で物理 drop する。

``articles_count`` / ``duration_ms`` のような事前計算済み指標は失うが、後者は
Logfire taskiq span (Phase 3 trace 伝搬) で自動記録され、前者は pipeline_events を
``event_type=succeeded AND outcome_code IN ('article_created',
'incomplete_article_created')`` で COUNT すれば導出可能。集計 consumer 出現時に
焼き直せばよく、先回り焼き付けはしない (consumer-driven audit scope)。

downgrade は a6 → 42a07fc6d36d → c18 → c19 の累積最終状態 (テーブル + 3 CHECK +
単列 ``ix_fetch_logs_source_id`` index) を 1 migration で復元する。

Revision ID: z7_drop_fetch_logs
Revises: z6_briefing_audit_setup
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z7_drop_fetch_logs"
down_revision: str | None = "z6_briefing_audit_setup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL は DROP TABLE で関連 index (ix_fetch_logs_source_id) と CHECK
    # 制約 (ck_fetch_logs_status / ck_fetch_logs_articles_count_non_negative /
    # ck_fetch_logs_duration_ms_non_negative) を自動 drop するため明示削除不要。
    op.drop_table("fetch_logs")


def downgrade() -> None:
    op.create_table(
        "fetch_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("news_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("articles_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('success', 'error')", name="ck_fetch_logs_status"
        ),
        sa.CheckConstraint(
            "articles_count >= 0",
            name="ck_fetch_logs_articles_count_non_negative",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_fetch_logs_duration_ms_non_negative",
        ),
    )
    op.create_index(
        "ix_fetch_logs_source_id", "fetch_logs", ["source_id"], unique=False
    )
