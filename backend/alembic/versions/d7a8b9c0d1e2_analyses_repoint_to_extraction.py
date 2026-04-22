"""article_analyses: repoint FK from articles to article_extractions, tighten NOT NULL.

article_analyses を「Stage 2（分類）が完了した Classified 専用」のテーブルに刷新する。
- article_id を廃止し、extraction_id への FK に置き換え（親は article_extractions）
- Stage 2 のフィールド（topic_id / impact_level / reasoning）を NOT NULL 化
- reasoning に NOT EMPTY CheckConstraint を再付与

Q1 の確認結果により Stage 1 only 行は 0 件だが、DELETE 文は防御的に残す（冪等で無害）。

Revision ID: d7a8b9c0d1e2
Revises: d3e4f5a6b7c8
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a8b9c0d1e2"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    # 1. extraction_id を nullable で追加し、article_id 経由でバックフィル
    op.add_column(
        "article_analyses",
        sa.Column("extraction_id", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE article_analyses aa
        SET extraction_id = e.id
        FROM article_extractions e
        WHERE e.article_id = aa.article_id;
        """
    )

    # 2. Stage 1 only 行の削除（Q1 で 0 件確認済みだが防御的に残す）
    op.execute("DELETE FROM article_analyses WHERE topic_id IS NULL;")

    # 3. NOT NULL + UNIQUE + FK
    op.alter_column("article_analyses", "extraction_id", nullable=False)
    op.create_unique_constraint(
        "uq_article_analyses_extraction_id",
        "article_analyses",
        ["extraction_id"],
    )
    op.create_foreign_key(
        "fk_article_analyses_extraction_id",
        "article_analyses",
        "article_extractions",
        ["extraction_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 4. 旧 article_id 関連を削除
    op.drop_constraint(
        "uq_article_analyses_article_id", "article_analyses", type_="unique"
    )
    op.drop_constraint(
        "fk_article_analyses_article_id", "article_analyses", type_="foreignkey"
    )
    op.drop_column("article_analyses", "article_id")

    # 5. Stage 2 フィールドを NOT NULL 化
    op.alter_column(
        "article_analyses",
        "topic_id",
        existing_type=sa.Integer(),
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
        "reasoning",
        existing_type=sa.Text(),
        nullable=False,
    )

    # 6. reasoning の NOT EMPTY 制約を再付与
    op.create_check_constraint(
        "ck_article_analyses_reasoning_not_empty",
        "article_analyses",
        "reasoning != ''",
    )


def downgrade() -> None:
    # reasoning CheckConstraint 撤去
    op.drop_constraint(
        "ck_article_analyses_reasoning_not_empty",
        "article_analyses",
        type_="check",
    )

    # NOT NULL を解除（Stage 2 未実行状態を再び許容）
    op.alter_column(
        "article_analyses",
        "reasoning",
        existing_type=sa.Text(),
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
        "topic_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    # article_id を再導入（extractions を介してバックフィル）
    op.add_column(
        "article_analyses",
        sa.Column("article_id", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE article_analyses aa
        SET article_id = e.article_id
        FROM article_extractions e
        WHERE aa.extraction_id = e.id;
        """
    )
    op.alter_column("article_analyses", "article_id", nullable=False)
    op.create_foreign_key(
        "fk_article_analyses_article_id",
        "article_analyses",
        "articles",
        ["article_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_article_analyses_article_id",
        "article_analyses",
        ["article_id"],
    )

    # extraction_id 関連を除去
    op.drop_constraint(
        "fk_article_analyses_extraction_id",
        "article_analyses",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_article_analyses_extraction_id",
        "article_analyses",
        type_="unique",
    )
    op.drop_column("article_analyses", "extraction_id")
