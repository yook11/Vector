"""add article_urls + pending_html_articles tables, articles.article_url_id column.

PR2.5-A (データモデル正常化の基盤):

- ``article_urls`` 新設 — URL identity 台帳 (一意性 SSoT、不変)
- ``pending_html_articles`` 新設 — Pattern H 用 HTML 取得待ちキュー (lease 方式)
- ``articles.article_url_id`` 追加 (nullable、UNIQUE + FK) + 既存 articles を
  全件 backfill

behavior 変更ゼロ。新テーブルは PR2.5-B の cutover まで誰も読み書きしない。
``articles`` の既存カラム (``discovered_article_id`` / ``source_url``) は
触らない。本 migration は完全可逆 (downgrade で全 DDL を反転)。

詳細は ``specs/pipeline-events-stage2-design.md`` 参照。

Revision ID: r1_pending_html_articles
Revises: o16_add_mdpi
Create Date: 2026-05-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "r1_pending_html_articles"
down_revision: str | None = "o16_add_mdpi"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. article_urls — URL identity 台帳
    op.create_table(
        "article_urls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("normalized_url", sa.String(length=2048), nullable=False),
        sa.Column("original_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "first_seen_source_id",
            sa.Integer(),
            sa.ForeignKey("news_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("normalized_url", name="uq_article_urls_normalized_url"),
        sa.CheckConstraint(
            "normalized_url ~ '^https?://.+'",
            name="ck_article_urls_normalized_url_scheme",
        ),
        sa.CheckConstraint(
            "original_url ~ '^https?://.+'",
            name="ck_article_urls_original_url_scheme",
        ),
    )

    # 2. pending_html_articles — HTML 取得待ちキュー (lease 方式)
    op.create_table(
        "pending_html_articles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "article_url_id",
            sa.BigInteger(),
            sa.ForeignKey("article_urls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("news_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "staged_attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "article_url_id", name="uq_pending_html_articles_article_url_id"
        ),
        sa.CheckConstraint(
            "status IN ('open','running','closed')",
            name="ck_pending_html_articles_status",
        ),
        sa.CheckConstraint(
            "(status = 'open'    AND leased_until IS NULL) OR "
            "(status = 'running' AND leased_until IS NOT NULL) OR "
            "(status = 'closed'  AND leased_until IS NULL)",
            name="ck_pending_html_articles_state_consistency",
        ),
        sa.CheckConstraint(
            "(status IN ('open','running') AND ready_at IS NOT NULL) OR "
            "(status = 'closed')",
            name="ck_pending_html_articles_ready_required",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_pending_html_articles_attempt_nonneg",
        ),
    )
    op.create_index(
        "ix_pending_html_articles_ready",
        "pending_html_articles",
        ["ready_at"],
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_pending_html_articles_expired_lease",
        "pending_html_articles",
        ["leased_until"],
        postgresql_where=sa.text("status = 'running'"),
    )

    # 3. articles.article_url_id (nullable) を追加
    op.add_column(
        "articles",
        sa.Column("article_url_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_articles_article_url_id",
        "articles",
        "article_urls",
        ["article_url_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_articles_article_url_id", "articles", ["article_url_id"]
    )

    # 4. 既存 articles を全件 backfill
    # (a) 重複検査: 同一 source_url が複数 articles 行に存在しないこと
    #     (articles.source_url UNIQUE で構造的に保証されているはずだが
    #      backfill 失敗を運用前に検出する保険)
    op.execute(
        sa.text(
            "DO $$ "
            "DECLARE dup_count int; "
            "BEGIN "
            "  SELECT COUNT(*) INTO dup_count FROM ("
            "    SELECT source_url FROM articles "
            "    GROUP BY source_url HAVING COUNT(*) > 1"
            "  ) sub; "
            "  IF dup_count > 0 THEN "
            "    RAISE EXCEPTION 'articles.source_url has % duplicates, "
            "abort backfill', dup_count; "
            "  END IF; "
            "END $$;"
        )
    )

    # (b) article_urls 行を articles から生成
    #     (既存 source_url は Stage 1 で正規化済の前提、再正規化はしない)
    op.execute(
        sa.text(
            "INSERT INTO article_urls "
            "(normalized_url, original_url, first_seen_source_id, first_seen_at) "
            "SELECT source_url, source_url, source_id, created_at "
            "FROM articles "
            "ON CONFLICT (normalized_url) DO NOTHING"
        )
    )

    # (c) articles.article_url_id を埋める
    op.execute(
        sa.text(
            "UPDATE articles a "
            "SET article_url_id = au.id "
            "FROM article_urls au "
            "WHERE au.normalized_url = a.source_url "
            "  AND a.article_url_id IS NULL"
        )
    )

    # (d) 検証: backfill 後に NULL が残っていないこと
    op.execute(
        sa.text(
            "DO $$ "
            "DECLARE unfilled int; "
            "BEGIN "
            "  SELECT COUNT(*) INTO unfilled FROM articles "
            "  WHERE article_url_id IS NULL; "
            "  IF unfilled > 0 THEN "
            "    RAISE EXCEPTION '% articles still have NULL "
            "article_url_id after backfill', unfilled; "
            "  END IF; "
            "END $$;"
        )
    )


def downgrade() -> None:
    # articles の変更を巻き戻す
    op.drop_constraint("uq_articles_article_url_id", "articles", type_="unique")
    op.drop_constraint("fk_articles_article_url_id", "articles", type_="foreignkey")
    op.drop_column("articles", "article_url_id")

    # 新設テーブルを drop
    op.drop_index(
        "ix_pending_html_articles_expired_lease",
        table_name="pending_html_articles",
    )
    op.drop_index("ix_pending_html_articles_ready", table_name="pending_html_articles")
    op.drop_table("pending_html_articles")
    op.drop_table("article_urls")
