"""Phase 1B α-1: 新テーブル ``article_extraction_entities`` を追加。

Stage 1 Extraction の刷新 (surface + raw_type の自由記述) に対応する観察台帳。
旧 ``article_entities`` は本 migration では DROP せず、l9 migration で別途削除する
(レビュー時に「ここから不可逆」が一目で分かるよう l9 を最後のコミットに置く設計)。

DDL:
- id: BIGSERIAL (再抽出運用で行数が累積するため余裕を持たせる)
- extraction_id: INTEGER (親 article_extractions.id が Integer なので一致)
- surface: VARCHAR(200)
- raw_type: VARCHAR(30)
- position: SMALLINT (AI 出力順を保存)
- created_at: TIMESTAMPTZ DEFAULT NOW()
- CHECK: surface != '' / raw_type != ''
- INDEX: (extraction_id) — extraction_id 単位の SELECT (集計 / 削除) で使う

Revision ID: l8_aee_create
Revises: fca0688c78ab
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "l8_aee_create"
down_revision = "fca0688c78ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "article_extraction_entities",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "extraction_id",
            sa.Integer(),
            sa.ForeignKey("article_extractions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("surface", sa.String(length=200), nullable=False),
        sa.Column("raw_type", sa.String(length=30), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("surface != ''", name="ck_aee_surface_not_empty"),
        sa.CheckConstraint("raw_type != ''", name="ck_aee_raw_type_not_empty"),
    )
    op.create_index(
        "ix_article_extraction_entities_extraction_id",
        "article_extraction_entities",
        ["extraction_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_article_extraction_entities_extraction_id",
        table_name="article_extraction_entities",
    )
    op.drop_table("article_extraction_entities")
