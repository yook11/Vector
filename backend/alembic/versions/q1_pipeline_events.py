"""add pipeline_events audit table.

PR1 (observability): 観測基盤の中心テーブル。append-only、全 9 Stage / 全 4
EventType を 1 行 = 1 イベントで扱う。Stage 1 (source_fetch) は本 PR で
書込開始、他 Stage は順次。

詳細は ``docs/observability/pipeline-events-design.md`` 参照。

Revision ID: q1_pipeline_events
Revises: p1_add_extraction_noises
Create Date: 2026-05-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "q1_pipeline_events"
down_revision: str | None = "p1_add_extraction_noises"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STAGE_VALUES = (
    "dispatch",
    "source_fetch",
    "content_fetch",
    "extraction",
    "classification",
    "embedding",
    "backfill_extract",
    "backfill_classify",
    "backfill_embed",
)
_EVENT_TYPE_VALUES = ("succeeded", "skipped", "rejected", "failed")


def upgrade() -> None:
    op.create_table(
        "pipeline_events",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("outcome_code", sa.String(length=60), nullable=False),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("news_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "attempt",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(length=160), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "stage IN (" + ",".join(f"'{v}'" for v in _STAGE_VALUES) + ")",
            name="ck_pipeline_events_stage",
        ),
        sa.CheckConstraint(
            "event_type IN (" + ",".join(f"'{v}'" for v in _EVENT_TYPE_VALUES) + ")",
            name="ck_pipeline_events_event_type",
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name="ck_pipeline_events_attempt_positive",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_pipeline_events_duration_nonneg",
        ),
    )

    # BRIN(occurred_at) — append-only な時系列データに最適 (ix だけ raw SQL)
    op.execute(
        "CREATE INDEX ix_pipeline_events_occurred_at_brin "
        "ON pipeline_events USING brin (occurred_at)"
    )
    op.create_index(
        "ix_pipeline_events_stage_outcome",
        "pipeline_events",
        ["stage", "event_type", "outcome_code", "occurred_at"],
    )
    op.create_index(
        "ix_pipeline_events_source_id",
        "pipeline_events",
        ["source_id", "occurred_at"],
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_pipeline_events_article_id",
        "pipeline_events",
        ["article_id", "occurred_at"],
        postgresql_where=sa.text("article_id IS NOT NULL"),
    )
    op.create_index(
        "ix_pipeline_events_failed",
        "pipeline_events",
        ["occurred_at"],
        postgresql_where=sa.text("event_type = 'failed'"),
    )
    op.create_index(
        "ix_pipeline_events_payload_gin",
        "pipeline_events",
        ["payload"],
        postgresql_using="gin",
        postgresql_ops={"payload": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_events_payload_gin", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_failed", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_article_id", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_source_id", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_stage_outcome", table_name="pipeline_events")
    op.execute("DROP INDEX IF EXISTS ix_pipeline_events_occurred_at_brin")
    op.drop_table("pipeline_events")
