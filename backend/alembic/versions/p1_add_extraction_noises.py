"""add extraction_noises table with mutual exclusion against article_extractions.

Stage 1 (Gemini extraction) signal/noise フィルタの noise 側永続化テーブル。
``article_extractions`` (signal) と排他関係を BEFORE INSERT/UPDATE トリガー
対称ペア + ``UNIQUE (article_id)`` で構造的に強制する
(``d5e6f7a8b9ca_add_exclusion_triggers.py`` と同型)。

設計:
- 1 article に対し ``article_extractions`` または ``extraction_noises`` の
  どちらか一方しか存在できない (DB トリガーで保証)
- ``entities`` は JSONB カラムとして同テーブルに内包する。noise 記事の
  entities は遡及検証 (プロンプト改訂時の ad-hoc 分析) 専用で個別 entity
  単位の JOIN/WHERE を想定しないため、子テーブル分離は採らない
  (feedback_snapshot_responsibility.md と整合)

Revision ID: p1_add_extraction_noises
Revises: o13_add_frontiers
Create Date: 2026-05-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "p1_add_extraction_noises"
down_revision: str | None = "o13_add_frontiers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction_noises",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "article_id",
            sa.BigInteger(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title_ja", sa.String(length=500), nullable=False),
        sa.Column("summary_ja", sa.Text(), nullable=False),
        sa.Column(
            "entities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("ai_model", sa.String(length=100), nullable=False),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("article_id", name="uq_extraction_noises_article_id"),
        sa.CheckConstraint(
            "title_ja <> ''",
            name="ck_extraction_noises_title_ja_not_empty",
        ),
        sa.CheckConstraint(
            "summary_ja <> ''",
            name="ck_extraction_noises_summary_ja_not_empty",
        ),
        sa.CheckConstraint(
            "ai_model <> ''",
            name="ck_extraction_noises_ai_model_not_empty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entities) = 'array'",
            name="ck_extraction_noises_entities_is_array",
        ),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_noise_for_extraction()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM extraction_noises
                WHERE article_id = NEW.article_id
            ) THEN
                RAISE EXCEPTION
                    'article % already has an extraction_noise', NEW.article_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_article_extractions_no_noise
        BEFORE INSERT OR UPDATE ON article_extractions
        FOR EACH ROW EXECUTE FUNCTION enforce_no_noise_for_extraction();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_extraction_for_noise()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM article_extractions
                WHERE article_id = NEW.article_id
            ) THEN
                RAISE EXCEPTION 'article % already has an extraction', NEW.article_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_extraction_noises_no_extraction
        BEFORE INSERT OR UPDATE ON extraction_noises
        FOR EACH ROW EXECUTE FUNCTION enforce_no_extraction_for_noise();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_extraction_noises_no_extraction "
        "ON extraction_noises;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_extraction_for_noise();")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_article_extractions_no_noise "
        "ON article_extractions;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_noise_for_extraction();")
    op.drop_table("extraction_noises")
