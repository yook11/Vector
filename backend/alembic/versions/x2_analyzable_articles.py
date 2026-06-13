"""rename articles table to analyzable_articles.

``articles`` は collection BC の出口契約 ``AnalyzableArticle`` を永続化した
record であり、public API の `/articles` や audit の横断 ``article_id`` と
語彙が衝突していた。DB table を ``analyzable_articles`` に改名し、ORM
``AnalyzableArticleRecord`` と揃える。

``pipeline_events.article_id`` は横断 correlation key として維持する。FK target
は table rename により ``analyzable_articles.id`` を指すが、column 名は変えない。

deploy は stop-the-world 前提。旧 backend が ``articles`` を参照したまま動くと
UndefinedTable になるため、全 process 停止 → migrate → 新 image 起動で適用する。

Revision ID: x2_analyzable_articles
Revises: w4_remap_key_article_ids
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "x2_analyzable_articles"
down_revision: str | None = "w4_remap_key_article_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: op.rename_table + constraint/index/sequence rename は contract。
MIGRATION_KIND = "contract"

_CONSTRAINT_RENAMES: tuple[tuple[str, str], ...] = (
    ("articles_pkey", "analyzable_articles_pkey"),
    ("fk_articles_source_id", "analyzable_articles_source_id_fkey"),
    ("uq_articles_source_url", "uq_analyzable_articles_source_url"),
    ("ck_articles_title_not_empty", "ck_analyzable_articles_title_not_empty"),
    (
        "ck_articles_source_url_scheme",
        "ck_analyzable_articles_source_url_scheme",
    ),
)

_INDEX_RENAMES: tuple[tuple[str, str], ...] = (
    ("idx_articles_published", "idx_analyzable_articles_published"),
    ("ix_articles_source_id", "ix_analyzable_articles_source_id"),
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    op.rename_table("articles", "analyzable_articles")

    for old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE analyzable_articles RENAME CONSTRAINT {old} TO {new};")

    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new};")

    op.execute("ALTER SEQUENCE articles_id_seq RENAME TO analyzable_articles_id_seq;")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    op.execute("ALTER SEQUENCE analyzable_articles_id_seq RENAME TO articles_id_seq;")

    for old, new in reversed(_INDEX_RENAMES):
        op.execute(f"ALTER INDEX {new} RENAME TO {old};")

    for old, new in reversed(_CONSTRAINT_RENAMES):
        op.execute(f"ALTER TABLE analyzable_articles RENAME CONSTRAINT {new} TO {old};")

    op.rename_table("analyzable_articles", "articles")
