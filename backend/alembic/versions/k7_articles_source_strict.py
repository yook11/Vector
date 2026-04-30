"""articles.source_id / source_url を NOT NULL + UNIQUE + index + CHECK に強化。

collection-acquisition-redesign Phase 0b。k6 (Phase 0a) でカラム追加 +
バックフィルまで済ませた前提で、本 migration は次を atomically にまとめる:

1. **再バックフィル** — k6 適用後 → k7 適用前の窓 (運用上 数日) で発生した
   新規 INSERT は ORM がまだ新カラムを埋めない (PR-0b で同時に ORM/Repo を
   修正するため) ので、残留 NULL を `discovered_articles` から JOIN UPDATE
   で吸収する。冪等。
2. **重複 / NULL 残留チェック** — UNIQUE 制約失敗 / NOT NULL 失敗を migration
   内で先回りに検出 (`DO $$ ... RAISE EXCEPTION` パターン、d1e2f3a4b5c6 / k6
   と一貫)。失敗時は人間が `articles` を手で詰める運用ランブックに切り替える。
3. **NOT NULL** — 両カラムを NOT NULL 化。
4. **UNIQUE / index / CHECK** — `source_url` UNIQUE (canonical URL の重複防止)、
   `source_id` index (news_sources JOIN の性能)、`source_url` の scheme
   ホワイトリスト CHECK (defense-in-depth、SafeUrl 型と一致)。

Revision ID: k7_articles_source_strict
Revises: k6_articles_source_cols
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op

revision = "k7_articles_source_strict"
down_revision = "k6_articles_source_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 再バックフィル (k6 → k7 の窓で残った NULL を吸収。冪等)
    op.execute(
        """
        UPDATE articles AS a
        SET source_id  = d.news_source_id,
            source_url = d.original_url
        FROM discovered_articles AS d
        WHERE a.discovered_article_id = d.id
          AND (a.source_id IS NULL OR a.source_url IS NULL)
        """
    )

    # 2. 事前検証 (NULL 残留 + UNIQUE 候補の重複)
    op.execute(
        """
        DO $$
        DECLARE
            null_count integer;
            dup_count integer;
        BEGIN
            SELECT COUNT(*) FILTER (WHERE source_id IS NULL OR source_url IS NULL)
              INTO null_count
              FROM articles;
            IF null_count > 0 THEN
                RAISE EXCEPTION
                    'null source_id/source_url remain: % rows', null_count;
            END IF;

            SELECT COUNT(*) INTO dup_count FROM (
                SELECT source_url
                FROM articles
                WHERE source_url IS NOT NULL
                GROUP BY source_url
                HAVING COUNT(*) > 1
            ) t;
            IF dup_count > 0 THEN
                RAISE EXCEPTION
                    'duplicate source_url found: % distinct urls', dup_count;
            END IF;
        END $$;
        """
    )

    # 3. NOT NULL 化
    op.alter_column("articles", "source_id", nullable=False)
    op.alter_column("articles", "source_url", nullable=False)

    # 4. UNIQUE / index / CHECK
    op.create_unique_constraint("uq_articles_source_url", "articles", ["source_url"])
    op.create_index("ix_articles_source_id", "articles", ["source_id"])
    op.create_check_constraint(
        "ck_articles_source_url_scheme",
        "articles",
        "source_url ~ '^https?://.+'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_articles_source_url_scheme", "articles", type_="check")
    op.drop_index("ix_articles_source_id", table_name="articles")
    op.drop_constraint("uq_articles_source_url", "articles", type_="unique")
    op.alter_column("articles", "source_url", nullable=True)
    op.alter_column("articles", "source_id", nullable=True)
