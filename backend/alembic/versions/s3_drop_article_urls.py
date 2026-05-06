"""``article_urls`` 系の物理 DROP (PR-F)。

PR-D / PR-E (`s1_pending_url_column` / `s2_articles_canonicalize`) で
``article_urls`` 経路は production code から完全に外れた。
``articles.source_url`` (canonicalize 済み SSoT) と
``pending_html_articles.url`` の各 UNIQUE が dedup の物理保証を担う。

本 migration では:

1. ``articles.article_url_id`` の UNIQUE / FK / 列を DROP
2. ``pending_html_articles.article_url_id`` の UNIQUE / FK / 列を DROP
3. ``article_urls`` テーブル本体を DROP

DROP 順序は r3 の前例 (``r3_drop_discovered_articles.py``) と完全同型 —
参照側 (UNIQUE → FK → column) → 被参照側 (table) の順。

downgrade は元 DDL の構造のみ復元する (``article_urls`` 行データおよび
``article_url_id`` の値は復元できない、許容)。type 復元は ``SafeUrlType``
ではなく ``sa.String(2048)`` を使う (alembic は時間軸を遡って実行可能で
あるべきで、runtime 型に依存させない)。CHECK 制約は同じ正規表現で復元
するので機能的に同等。

Revision ID: s3_drop_article_urls
Revises: s2_articles_canonicalize
Create Date: 2026-05-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "s3_drop_article_urls"
down_revision: str | None = "s2_articles_canonicalize"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 制約名は r1_create_article_urls / PR-D / ORM `__table_args__` の
# ``UniqueConstraint(name=...)`` で明示済 + Postgres 自動命名の FK。
_ARTICLES_UQ = "uq_articles_article_url_id"
_ARTICLES_FK = "fk_articles_article_url_id"
_PENDING_UQ = "uq_pending_html_articles_article_url_id"
_PENDING_FK = "pending_html_articles_article_url_id_fkey"


def upgrade() -> None:
    # 1. articles 側を切り離す (UNIQUE → FK → column)
    op.drop_constraint(_ARTICLES_UQ, "articles", type_="unique")
    op.drop_constraint(_ARTICLES_FK, "articles", type_="foreignkey")
    op.drop_column("articles", "article_url_id")

    # 2. pending_html_articles 側を切り離す
    op.drop_constraint(_PENDING_UQ, "pending_html_articles", type_="unique")
    op.drop_constraint(_PENDING_FK, "pending_html_articles", type_="foreignkey")
    op.drop_column("pending_html_articles", "article_url_id")

    # 3. article_urls 本体を DROP (もう参照する FK は無い)
    op.drop_table("article_urls")


def downgrade() -> None:
    # 1. article_urls テーブルを構造復元 (SafeUrlType ではなく sa.String で代替)
    op.create_table(
        "article_urls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("normalized_url", sa.String(2048), nullable=False),
        sa.Column("original_url", sa.String(2048), nullable=False),
        sa.Column(
            "first_seen_source_id",
            sa.Integer(),
            sa.ForeignKey("news_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
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

    # 2. articles.article_url_id を nullable で復元 (PR-E 後の状態に揃える)
    op.add_column(
        "articles",
        sa.Column("article_url_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        _ARTICLES_FK,
        "articles",
        "article_urls",
        ["article_url_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(_ARTICLES_UQ, "articles", ["article_url_id"])

    # 3. pending_html_articles.article_url_id を nullable で復元
    op.add_column(
        "pending_html_articles",
        sa.Column("article_url_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        _PENDING_FK,
        "pending_html_articles",
        "article_urls",
        ["article_url_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        _PENDING_UQ, "pending_html_articles", ["article_url_id"]
    )
