"""articles に source_id / source_url カラムを追加し既存行をバックフィルする。

collection-acquisition-redesign Phase 0a。本リファクタリングでは acquisition の
出口を `FetchedArticle` 型 (source_id + source_url を直接保持) で固定するため、
既存 articles に news_sources への直接 FK と canonical URL を持たせる必要がある。

本 migration は **NULL 許容で追加 + 既存行のバックフィル** までを行い、NOT NULL
化と UNIQUE 制約付与は次の k7 (Phase 0b) で実施する。これは PR-0a 適用後 →
PR-0b 適用前の窓で新規 INSERT が走っても (ORM はまだ新カラムを埋めない) 失敗
させないための段階的設計 (spec collection-acquisition-redesign-plan.md §PR-0a)。

バックフィル元は `discovered_articles` (1:1 で articles.discovered_article_id
が指す行)。既存 articles は migration `d1e2f3a4b5c6` の経緯で全行が
discovered_article_id を埋めているため JOIN バックフィルで完全に埋まる。

事前検証 (`DO $$ ... RAISE EXCEPTION`) は migration d1e2f3a4b5c6 のパターンを
踏襲して migration 内で件数を確認、未バックフィル行があれば失敗させる。

Revision ID: k6_articles_source_cols
Revises: j5_fix_biopharma_dive_inactive
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "k6_articles_source_cols"
down_revision = "j5_fix_biopharma_dive_inactive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新カラム追加 (NULL 許容、まだ未使用)
    op.add_column(
        "articles",
        sa.Column("source_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "articles",
        sa.Column("source_url", sa.String(length=2048), nullable=True),
    )
    op.create_foreign_key(
        "fk_articles_source_id",
        "articles",
        "news_sources",
        ["source_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 2. バックフィル (discovered_articles から JOIN)
    op.execute(
        """
        UPDATE articles AS a
        SET source_id  = d.news_source_id,
            source_url = d.original_url
        FROM discovered_articles AS d
        WHERE a.discovered_article_id = d.id
        """
    )

    # 3. 事前検証: NULL 残留があれば失敗 (defense-in-depth)
    op.execute(
        """
        DO $$
        DECLARE
            null_source_id_count integer;
            null_source_url_count integer;
        BEGIN
            SELECT
                COUNT(*) FILTER (WHERE source_id IS NULL),
                COUNT(*) FILTER (WHERE source_url IS NULL)
            INTO null_source_id_count, null_source_url_count
            FROM articles;

            IF null_source_id_count > 0 OR null_source_url_count > 0 THEN
                RAISE EXCEPTION
                    'backfill incomplete: source_id_null=%, source_url_null=%',
                    null_source_id_count, null_source_url_count;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_articles_source_id", "articles", type_="foreignkey")
    op.drop_column("articles", "source_url")
    op.drop_column("articles", "source_id")
