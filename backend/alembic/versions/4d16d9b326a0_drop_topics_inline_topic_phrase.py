"""drop topics inline topic phrase

topics テーブルを廃止し、article_analyses.topic（自由記述、TopicName VO 列）と
category_id（FK→categories.id, RESTRICT, NOT NULL）を新設する。
既存 analyses / rejections は wipe され watchlist は CASCADE で巻き込まれる
（開発段階のため許容、d3996317ee0b と同方針）。

順序の根拠:
- topic_id FK は ondelete=RESTRICT のため、行が残ったまま topics を消せない
- 先に analyses 全行を DELETE し、次に topic_id 列を drop し、最後に topics を drop
- rejections は FK 依存ではなく業務的 wipe（再分類対象に戻すため）
- watchlist_entries は article_analyses の CASCADE で巻き込まれる

Revision ID: 4d16d9b326a0
Revises: f5a3c8e9b2d1
Create Date: 2026-04-25

"""

import os

import sqlalchemy as sa
from alembic import op

revision: str = "4d16d9b326a0"
down_revision: str | None = "f5a3c8e9b2d1"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    # Environment guard: prevent accidental destructive run via Docker entrypoint
    if os.environ.get("ALEMBIC_ALLOW_DESTRUCTIVE") != "yes-i-know":
        raise RuntimeError(
            "Refusing to run destructive migration. "
            "Set ALEMBIC_ALLOW_DESTRUCTIVE=yes-i-know in your shell only."
        )

    # Match prior migration convention (f5a3c8e9b2d1, 9304ea71c183).
    op.execute("SET lock_timeout = '5s'")

    bind = op.get_bind()

    # Observability: record wipe baseline.
    n_analyses = bind.execute(
        sa.text("SELECT count(*) FROM article_analyses")
    ).scalar_one()
    n_rejections = bind.execute(
        sa.text("SELECT count(*) FROM article_rejections")
    ).scalar_one()
    print(
        f"[migration] wipe baseline: analyses={n_analyses}, "
        f"rejections={n_rejections}"
    )

    # Business wipe: re-analysis is required for everything.
    op.execute("DELETE FROM article_analyses;")
    op.execute("DELETE FROM article_rejections;")

    # Drop the prior composite index before removing topic_id.
    op.drop_index(
        "ix_article_analyses_topic_id_analyzed_at",
        table_name="article_analyses",
    )

    # Look up the FK name dynamically (no hardcoded names).
    fk_row = bind.execute(
        sa.text(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'article_analyses'::regclass
              AND contype = 'f'
              AND pg_get_constraintdef(oid) LIKE '%REFERENCES topics(id)%'
            """
        )
    ).fetchone()
    if fk_row is None:
        raise RuntimeError(
            "Could not locate FK from article_analyses.topic_id to topics."
        )
    op.drop_constraint(fk_row[0], "article_analyses", type_="foreignkey")

    # Drop topic_id (the standalone ix_article_analyses_topic_id is removed by
    # the column drop cascade; we don't need an explicit drop_index for it).
    op.drop_column("article_analyses", "topic_id")

    # Add the new free-text topic column (matches TopicName max_length=100).
    op.add_column(
        "article_analyses",
        sa.Column("topic", sa.String(length=100), nullable=False),
    )
    # Defense in depth #1: reject empty strings.
    op.create_check_constraint(
        "ck_article_analyses_topic_not_empty",
        "article_analyses",
        "topic <> ''",
    )
    # Defense in depth #2: enforce TopicName VO format at the DB layer too
    # (lowercase a-z0-9 tokens, single space separated, max 3 words).
    op.create_check_constraint(
        "ck_article_analyses_topic_format",
        "article_analyses",
        r"topic ~ '^[a-z0-9]+( [a-z0-9]+){0,2}$'",
    )

    # Add category_id (first-class filter axis, replaces topic-mediated filter).
    op.add_column(
        "article_analyses",
        sa.Column("category_id", sa.Integer(), nullable=False),
    )
    op.create_foreign_key(
        "fk_article_analyses_category_id_categories",
        "article_analyses",
        "categories",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # New composite index for sidebar / list queries.
    # We intentionally don't create a standalone ix_article_analyses_category_id
    # because the composite covers left-prefix lookups by category_id.
    op.create_index(
        "ix_article_analyses_category_id_analyzed_at",
        "article_analyses",
        ["category_id", "analyzed_at"],
    )

    # Drop the topics table itself (FK already removed).
    op.drop_table("topics")


def downgrade() -> None:
    """Downgrade is unsupported; topic / analysis data is non-recoverable."""
    raise NotImplementedError(
        "Downgrade is not supported: topics table contents are non-recoverable. "
        "Re-create via a fresh migration if needed."
    )
