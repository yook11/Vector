"""``discovered_articles`` 系撤去 (PR2.5-C)。

PR2.5-B (cutover) で Pattern H 経路は ``article_urls`` + ``pending_html_articles``
3 表駆動に切り替わり、``discovered_articles`` テーブルおよび
``articles.discovered_article_id`` 列は dead code 化した。本 migration で
物理削除する。

upgrade は参照側 → 被参照側の順:
    1. UNIQUE ``uq_articles_discovered_article_id`` を drop
    2. FK ``articles_discovered_article_id_fkey`` を drop
    3. ``articles.discovered_article_id`` 列を drop
    4. ``discovered_articles`` テーブルを drop

downgrade は元の DDL を再現する (FK ondelete=SET NULL は r2 が張った状態に
合わせる)。``discovered_articles`` 行データおよび
``articles.discovered_article_id`` の値は復元できない (許容: 本番は前方一方通行、
ローカル round-trip 確認用途)。

Revision ID: r3_drop_discovered_articles
Revises: r2_articles_disc_nullable
Create Date: 2026-05-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "r3_drop_discovered_articles"
down_revision: str | None = "r2_articles_disc_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# r2 と同一の Postgres 自動命名 (alembic d1e2f3a4b5c6 で name 指定なしで作成)
_FK_NAME = "articles_discovered_article_id_fkey"
_UQ_NAME = "uq_articles_discovered_article_id"


def upgrade() -> None:
    # 1. UNIQUE 制約 drop (FK より先に外しておく)
    op.drop_constraint(_UQ_NAME, "articles", type_="unique")
    # 2. FK drop
    op.drop_constraint(_FK_NAME, "articles", type_="foreignkey")
    # 3. articles.discovered_article_id 列 drop
    op.drop_column("articles", "discovered_article_id")
    # 4. discovered_articles テーブル drop
    op.drop_table("discovered_articles")


def downgrade() -> None:
    # 1. discovered_articles テーブルを復元 (元 DDL は d1e2f3a4b5c6 と同等)
    op.create_table(
        "discovered_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "news_source_id",
            sa.Integer(),
            sa.ForeignKey("news_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("original_url", sa.String(), nullable=False),
        sa.Column("original_title", sa.String(500), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("original_url", name="uq_discovered_articles_original_url"),
        sa.CheckConstraint(
            "original_url ~ '^https?://.+'",
            name="ck_discovered_articles_url_scheme",
        ),
        sa.CheckConstraint(
            "original_title != ''",
            name="ck_discovered_articles_title_not_empty",
        ),
    )
    # 2. articles.discovered_article_id 列を nullable で復元
    op.add_column(
        "articles",
        sa.Column("discovered_article_id", sa.Integer(), nullable=True),
    )
    # 3. FK を ondelete=SET NULL で復元 (r2 upgrade 後の状態に合わせる)
    op.create_foreign_key(
        _FK_NAME,
        "articles",
        "discovered_articles",
        ["discovered_article_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # 4. UNIQUE 制約を復元
    op.create_unique_constraint(
        _UQ_NAME,
        "articles",
        ["discovered_article_id"],
    )
