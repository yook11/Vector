"""add query_embedding_cache table

内部検索 query 埋め込みの exact-match キャッシュ。(query_hash, embedder_identity)
で引き、平文 query は保存しない (hash と vector のみ)。

Revision ID: x7_query_embedding_cache
Revises: t3_curation_noise_rename
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import HALFVEC

from alembic import op

revision: str = "x7_query_embedding_cache"
down_revision: str | None = "t3_curation_noise_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: 新規テーブル追加のみ (op.create_table 単独、破壊系/op.execute なし)。
MIGRATION_KIND = "expand"


def upgrade() -> None:
    op.create_table(
        "query_embedding_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("embedder_identity", sa.String(length=255), nullable=False),
        sa.Column("query_vector", HALFVEC(768), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "query_hash",
            "embedder_identity",
            name="uq_query_embedding_cache_hash_identity",
        ),
        sa.CheckConstraint(
            "char_length(query_hash) = 64",
            name="ck_query_embedding_cache_query_hash_len",
        ),
    )


def downgrade() -> None:
    op.drop_table("query_embedding_cache")
