"""Phase 1B-γ-1: ``weekly_briefings`` テーブルを作成。

カテゴリ単位の週次 LLM 解説 (DeepSeek-V4 Pro) を 1 行 1 ブリーフィングとして
保持する。``stories`` は ``WeeklyBriefingContent.stories`` をそのまま JSONB に
格納し、``headline`` / ``model_name`` / ``input_article_count`` 等の検索/監査
属性のみカラム化する (snapshot とは異なり LLM 出力自身が source なので、
メタ属性はカラム抽出が必要: feedback_briefing_design_lessons.md)。

設計:
- ``UNIQUE (week_start_date, category_id)`` でカテゴリ × 週の重複を構造的に防ぐ
- ``ix_weekly_briefings_category_week`` は「カテゴリの最新 briefing 取得」
  クエリ (``WHERE category_id = ? ORDER BY week_start_date DESC LIMIT 1``)
  の高速化用
- ``input_article_count >= 0`` を CHECK で構造的に強制
- ``categories.id`` への FK で削除時挙動は restrict (default)

Revision ID: m1_briefings_create
Revises: l9_ae_drop
Create Date: 2026-05-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "m1_briefings_create"
down_revision: str | None = "l9_ae_drop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weekly_briefings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("week_start_date", sa.Date(), nullable=False),
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("categories.id"),
            nullable=False,
        ),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column(
            "stories",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("input_article_count", sa.Integer(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "week_start_date",
            "category_id",
            name="uq_weekly_briefing",
        ),
        sa.CheckConstraint(
            "input_article_count >= 0",
            name="ck_weekly_briefings_count_non_negative",
        ),
    )
    op.create_index(
        "ix_weekly_briefings_category_week",
        "weekly_briefings",
        ["category_id", sa.text("week_start_date DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_weekly_briefings_category_week",
        table_name="weekly_briefings",
    )
    op.drop_table("weekly_briefings")
