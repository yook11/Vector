"""分析パイプラインを2段階に分離 — nullable化 + article_entities テーブル追加。

ArticleAnalysis の topic_id / impact_level / reasoning を nullable に変更し、
Stage 1（抽出）完了・Stage 2（分類）未完了の中間状態を許容する。
エンティティ抽出結果を保存する article_entities テーブルを新設。

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- ArticleAnalysis: reasoning の NOT EMPTY 制約を削除 ---
    op.drop_constraint(
        "ck_article_analyses_reasoning_not_empty", "article_analyses", type_="check"
    )

    # --- ArticleAnalysis: nullable 化 ---
    op.alter_column(
        "article_analyses",
        "topic_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.alter_column(
        "article_analyses",
        "impact_level",
        existing_type=sa.String(20),
        nullable=True,
    )
    op.alter_column(
        "article_analyses",
        "reasoning",
        existing_type=sa.Text(),
        nullable=True,
    )

    # --- article_entities テーブル新設 ---
    op.create_table(
        "article_entities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_analysis_id",
            sa.Integer(),
            sa.ForeignKey("article_analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.CheckConstraint(
            "type IN ('company', 'product', 'technology')",
            name="ck_article_entities_type_valid",
        ),
    )
    op.create_index(
        "ix_article_entities_article_analysis_id",
        "article_entities",
        ["article_analysis_id"],
    )
    op.create_index(
        "ix_article_entities_name_type",
        "article_entities",
        ["name", "type"],
    )


def downgrade() -> None:
    # --- article_entities テーブル削除 ---
    op.drop_index("ix_article_entities_name_type", table_name="article_entities")
    op.drop_index(
        "ix_article_entities_article_analysis_id", table_name="article_entities"
    )
    op.drop_table("article_entities")

    # --- ArticleAnalysis: NOT NULL に戻す ---
    op.alter_column(
        "article_analyses",
        "reasoning",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "article_analyses",
        "impact_level",
        existing_type=sa.String(20),
        nullable=False,
    )
    op.alter_column(
        "article_analyses",
        "topic_id",
        existing_type=sa.Integer(),
        nullable=False,
    )

    # --- reasoning の NOT EMPTY 制約を復元 ---
    op.create_check_constraint(
        "ck_article_analyses_reasoning_not_empty",
        "article_analyses",
        "reasoning != ''",
    )
