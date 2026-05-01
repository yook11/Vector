"""Phase 1B α-1: 旧テーブル ``article_entities`` を DROP (clean break の最終段)。

l8 で新テーブル ``article_extraction_entities`` を作成し、ORM / Repository /
Snapshot / CLI を新スキーマへ切替済み。本 migration は旧テーブルを完全に
撤去する不可逆オペレーション。

l9 を最後のコミットに置くことで、レビュー時に「ここから不可逆」の境界が
コミットログ上で一目で分かる設計 (deploy 直前に ``pg_dump --table=article_entities``
取得を runbook で要求する)。

DROP 内容:
- table ``article_entities``
- index ``ix_article_entities_article_extraction_id``
- index ``ix_article_entities_name_type``
- FK constraint ``fk_article_entities_article_extraction_id`` (テーブル削除で
  自動消滅)

Revision ID: l9_ae_drop
Revises: l8_aee_create
Create Date: 2026-05-01
"""

from __future__ import annotations

from alembic import op

revision = "l9_ae_drop"
down_revision = "l8_aee_create"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_article_entities_name_type", table_name="article_entities")
    op.drop_index(
        "ix_article_entities_article_extraction_id",
        table_name="article_entities",
    )
    op.drop_table("article_entities")


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported: article_entities data is dropped and "
        "not recoverable. Restore from pg_dump if rollback is required."
    )
