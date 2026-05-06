"""articles.discovered_article_id を nullable + FK ondelete を SET NULL に変更.

PR2.5-B (cutover) で新規 articles INSERT 時に discovered_article_id=NULL を入れる
ため、NOT NULL 制約を外す。

加えて、deploy 手順の ``TRUNCATE TABLE discovered_articles ... CASCADE`` が
``articles`` を巻き込まないよう FK ondelete を CASCADE → SET NULL に変更する
(旧 articles 行は ``discovered_article_id`` が NULL になるが
``article_url_id`` は PR2.5-A backfill 済の値を保持する)。

``discovered_article_id`` カラム / FK 自体の DROP は PR2.5-C で行う。
完全可逆 (downgrade で nullable=False + ondelete=CASCADE に戻す)。

Revision ID: r2_articles_disc_nullable
Revises: r1_pending_html_articles
Create Date: 2026-05-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "r2_articles_disc_nullable"
down_revision: str | None = "r1_pending_html_articles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Postgres auto-generated FK name (alembic d1e2f3a4b5c6 で name 指定なしで作成)
_FK_NAME = "articles_discovered_article_id_fkey"


def upgrade() -> None:
    # 1. FK を CASCADE → SET NULL に張り直す
    op.drop_constraint(_FK_NAME, "articles", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        "articles",
        "discovered_articles",
        ["discovered_article_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # 2. NOT NULL → nullable
    op.alter_column(
        "articles",
        "discovered_article_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # NULL 行があると nullable=False への戻し alter は失敗する
    # (本 PR で発生した NULL 行は PR2.5-C で列削除されるため downgrade で
    # 当該 PR より前に戻すことは想定しない)。
    op.alter_column(
        "articles",
        "discovered_article_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_constraint(_FK_NAME, "articles", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        "articles",
        "discovered_articles",
        ["discovered_article_id"],
        ["id"],
        ondelete="CASCADE",
    )
