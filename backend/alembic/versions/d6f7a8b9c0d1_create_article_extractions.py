"""create article_extractions and copy Stage 1 fields from article_analyses.

Stage 1（事実抽出）の成果物を独立テーブルに切り出す。
既存の article_analyses からは translated_title / summary / ai_model / analyzed_at を
新テーブルに複製するが、article_analyses 側のカラムはまだ残す（後続 revision で FK 付け替え後に刷新）。

Revision ID: d6f7a8b9c0d1
Revises: 1250a92960a5
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6f7a8b9c0d1"
down_revision: str | None = "1250a92960a5"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_extractions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("translated_title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("ai_model", sa.String(100), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("article_id", name="uq_article_extractions_article_id"),
        sa.CheckConstraint(
            "translated_title != ''",
            name="ck_article_extractions_translated_title_not_empty",
        ),
        sa.CheckConstraint(
            "summary != ''",
            name="ck_article_extractions_summary_not_empty",
        ),
        sa.CheckConstraint(
            "ai_model != ''",
            name="ck_article_extractions_ai_model_not_empty",
        ),
    )

    # 既存 analysis 行から Stage 1 フィールドを COPY（Q2）
    op.execute(
        """
        INSERT INTO article_extractions (
            article_id, translated_title, summary, ai_model, extracted_at
        )
        SELECT article_id, translated_title, summary, ai_model, analyzed_at
        FROM article_analyses;
        """
    )


def downgrade() -> None:
    op.drop_table("article_extractions")
