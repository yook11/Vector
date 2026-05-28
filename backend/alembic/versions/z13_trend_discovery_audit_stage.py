"""add trend_discovery pipeline event stage.

Revision ID: z13_trend_discovery_audit_stage
Revises: z12_add_backfill_exclusions
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z13_trend_discovery_audit_stage"
down_revision: str | None = "z12_add_backfill_exclusions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGE_VALUES_OLD: tuple[str, ...] = (
    "dispatch",
    "acquisition",
    "completion",
    "curation",
    "assessment",
    "embedding",
    "backfill_curate",
    "backfill_assess",
    "backfill_embed",
    "briefing",
)

_STAGE_VALUES_NEW: tuple[str, ...] = (
    *_STAGE_VALUES_OLD,
    "trend_discovery",
)


def _stage_check_sql(values: tuple[str, ...]) -> str:
    return "stage IN (" + ",".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_NEW),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_OLD),
    )
