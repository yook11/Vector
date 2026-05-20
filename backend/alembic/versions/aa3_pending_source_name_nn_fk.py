"""enforce pending_html_articles.source_name NOT NULL + composite FK (step 3c).

PendingHtmlArticle の source identity を表層列に昇格する 3 段 migration の
**Step 3c**。3a (列追加 nullable) + 3b (backfill) を前提に、最終形の構造
保証を立てる:

1. ``news_sources`` に ``(id, name)`` UNIQUE を追加 — composite FK の参照先
   invariant。PK の ``id`` 単独で一意だが、PostgreSQL の composite FK target
   は明示的な UNIQUE / PK 制約を要するため必要。
2. ``pending_html_articles.source_name`` を NOT NULL に。
3. composite FK ``(source_id, source_name) → news_sources(id, name)`` を張る。
   ``ON DELETE RESTRICT`` (既存単独 FK と整合、news_sources は論理削除運用)、
   ``ON UPDATE RESTRICT`` (SourceName VO 不変条件 + news_sources.name 不変
   前提を構造で語る、変えたいなら明示的に作業)。

既存の単独 FK ``source_id → news_sources.id`` は **維持**。composite FK が
``source_id`` 整合も保証するため redundant だが、rollback 単位を細かく保ち
読み手に「source_id は news_sources の id を指す」という単独不変条件を
明示する。

Revision ID: aa3_pending_source_name_nn_fk
Revises: aa2_pending_source_name_backfill
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa3_pending_source_name_nn_fk"
down_revision: str | None = "aa2_pending_source_name_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # 1. news_sources に (id, name) UNIQUE を追加 (composite FK の参照先)。
    op.create_unique_constraint(
        "uq_news_sources_id_name",
        "news_sources",
        ["id", "name"],
    )

    # 2. pending_html_articles.source_name を NOT NULL に。
    #    3b で全行 backfill 済の前提 (3b 内の検証ブロックで NULL 行 0 を確認済)。
    op.alter_column(
        "pending_html_articles",
        "source_name",
        nullable=False,
    )

    # 3. composite FK (source_id, source_name) → news_sources(id, name)。
    op.create_foreign_key(
        "fk_pending_html_articles_source_id_name",
        "pending_html_articles",
        "news_sources",
        ["source_id", "source_name"],
        ["id", "name"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_pending_html_articles_source_id_name",
        "pending_html_articles",
        type_="foreignkey",
    )
    op.alter_column(
        "pending_html_articles",
        "source_name",
        nullable=True,
    )
    op.drop_constraint(
        "uq_news_sources_id_name",
        "news_sources",
        type_="unique",
    )
