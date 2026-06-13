"""rename curation state FK columns to analyzable_article_id.

``article_curations`` / ``curation_noises`` は public article ではなく
``analyzable_articles`` にぶら下がる Stage 3 state table なので、物理 FK column
だけ ``analyzable_article_id`` に揃える。

public API、task message、audit/Logfire の ``article_id`` は維持する。ここで変える
のは DB column と ORM field だけで、横断追跡キーとしての ``article_id`` は
``pipeline_events.article_id`` に残す。

Revision ID: x3_analyzable_article_fks
Revises: x2_analyzable_articles
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "x3_analyzable_article_fks"
down_revision: str | None = "x2_analyzable_articles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: alter_column(new_column_name=...) + trigger body rewrite は contract。
MIGRATION_KIND = "contract"

_CONSTRAINT_RENAMES: tuple[tuple[str, str, str], ...] = (
    (
        "article_curations",
        "uq_article_curations_article_id",
        "uq_article_curations_analyzable_article_id",
    ),
    (
        "article_curations",
        "fk_article_curations_article_id",
        "fk_article_curations_analyzable_article_id",
    ),
    (
        "curation_noises",
        "uq_curation_noises_article_id",
        "uq_curation_noises_analyzable_article_id",
    ),
    (
        "curation_noises",
        "fk_curation_noises_article_id",
        "fk_curation_noises_analyzable_article_id",
    ),
)


def _drop_curation_exclusion_triggers() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_curation_noises_no_curation ON curation_noises;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_article_curations_no_curation_noise "
        "ON article_curations;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_curation_for_curation_noise();")
    op.execute("DROP FUNCTION IF EXISTS enforce_no_curation_noise_for_curation();")


def _create_curation_exclusion_triggers(*, column_name: str) -> None:
    if column_name == "analyzable_article_id":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_no_curation_noise_for_curation()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM curation_noises
                    WHERE analyzable_article_id = NEW.analyzable_article_id
                ) THEN
                    RAISE EXCEPTION
                        'article % already has a curation_noise',
                        NEW.analyzable_article_id
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    else:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_no_curation_noise_for_curation()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM curation_noises
                    WHERE article_id = NEW.article_id
                ) THEN
                    RAISE EXCEPTION
                        'article % already has a curation_noise', NEW.article_id
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_article_curations_no_curation_noise
        BEFORE INSERT OR UPDATE ON article_curations
        FOR EACH ROW EXECUTE FUNCTION enforce_no_curation_noise_for_curation();
        """
    )
    if column_name == "analyzable_article_id":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_no_curation_for_curation_noise()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM article_curations
                    WHERE analyzable_article_id = NEW.analyzable_article_id
                ) THEN
                    RAISE EXCEPTION
                        'article % already has a curation',
                        NEW.analyzable_article_id
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    else:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_no_curation_for_curation_noise()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM article_curations
                    WHERE article_id = NEW.article_id
                ) THEN
                    RAISE EXCEPTION 'article % already has a curation', NEW.article_id
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_curation_noises_no_curation
        BEFORE INSERT OR UPDATE ON curation_noises
        FOR EACH ROW EXECUTE FUNCTION enforce_no_curation_for_curation_noise();
        """
    )


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    _drop_curation_exclusion_triggers()

    op.alter_column(
        "article_curations",
        "article_id",
        new_column_name="analyzable_article_id",
    )
    op.alter_column(
        "curation_noises",
        "article_id",
        new_column_name="analyzable_article_id",
    )

    for table, old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new};")

    _create_curation_exclusion_triggers(column_name="analyzable_article_id")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    _drop_curation_exclusion_triggers()

    for table, old, new in reversed(_CONSTRAINT_RENAMES):
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old};")

    op.alter_column(
        "curation_noises",
        "analyzable_article_id",
        new_column_name="article_id",
    )
    op.alter_column(
        "article_curations",
        "analyzable_article_id",
        new_column_name="article_id",
    )

    _create_curation_exclusion_triggers(column_name="article_id")
