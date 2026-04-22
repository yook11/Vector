"""repoint article_entities FK from article_analyses to article_extractions.

entities は Stage 1 の成果物であるため、extractions 配下に属するのが正。
rev_A で article_extractions を作成済みなので、この revision で FK を付け替える。

Revision ID: d3e4f5a6b7c8
Revises: d2e3f4a5b6c7
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    # 新カラム追加（nullable で一時的に）
    op.add_column(
        "article_entities",
        sa.Column("article_extraction_id", sa.Integer(), nullable=True),
    )

    # 既存 entity 行を extraction 側に紐付け直す
    op.execute(
        """
        UPDATE article_entities ae
        SET article_extraction_id = e.id
        FROM article_analyses aa
        JOIN article_extractions e ON e.article_id = aa.article_id
        WHERE ae.article_analysis_id = aa.id;
        """
    )

    # NOT NULL 化と新 FK
    op.alter_column("article_entities", "article_extraction_id", nullable=False)
    op.create_foreign_key(
        "fk_article_entities_article_extraction_id",
        "article_entities",
        "article_extractions",
        ["article_extraction_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 旧 FK・インデックス・カラムを削除
    op.drop_index(
        "ix_article_entities_article_analysis_id",
        table_name="article_entities",
    )
    # 1ee30c910254 の create_table で inline ForeignKey を張っているため
    # 制約名はデフォルト生成（PostgreSQL の慣例: <table>_<col>_fkey）
    op.drop_constraint(
        "article_entities_article_analysis_id_fkey",
        "article_entities",
        type_="foreignkey",
    )
    op.drop_column("article_entities", "article_analysis_id")

    # 新インデックス
    op.create_index(
        "ix_article_entities_article_extraction_id",
        "article_entities",
        ["article_extraction_id"],
    )


def downgrade() -> None:
    op.add_column(
        "article_entities",
        sa.Column("article_analysis_id", sa.Integer(), nullable=True),
    )

    # extraction → analysis 逆方向で復元
    op.execute(
        """
        UPDATE article_entities ae
        SET article_analysis_id = aa.id
        FROM article_extractions e
        JOIN article_analyses aa ON aa.article_id = e.article_id
        WHERE ae.article_extraction_id = e.id;
        """
    )

    op.alter_column("article_entities", "article_analysis_id", nullable=False)
    op.create_foreign_key(
        "article_entities_article_analysis_id_fkey",
        "article_entities",
        "article_analyses",
        ["article_analysis_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_article_entities_article_analysis_id",
        "article_entities",
        ["article_analysis_id"],
    )

    op.drop_index(
        "ix_article_entities_article_extraction_id",
        table_name="article_entities",
    )
    op.drop_constraint(
        "fk_article_entities_article_extraction_id",
        "article_entities",
        type_="foreignkey",
    )
    op.drop_column("article_entities", "article_extraction_id")
