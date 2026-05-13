"""Drop in_scope_assessments.topic column (event-extraction migration final).

PR 3 of event-extraction migration. topic は移行期 deprecated 並列出力で、
Stage 4 出力は (category, investor_take, events) の 3 フィールドに収束する。

upgrade:
  1. in_scope_assessments.topic に紐づく 2 CHECK 制約を drop
  2. in_scope_assessments.topic カラム drop

downgrade:
  schema 再作成のみで過去データは復元不可。production rollback は実質
  サポート外 (dev verification で十分検証してから merge する前提)。

Revision ID: a3_drop_assessment_topic
Revises: a2_drop_extraction_entities
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3_drop_assessment_topic"
down_revision: str | None = "a2_drop_extraction_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # deploy window 内で他 tx が長く lock を握る事故を予防
    op.execute("SET lock_timeout = '5s';")

    op.drop_constraint(
        "ck_in_scope_assessments_topic_format",
        "in_scope_assessments",
        type_="check",
    )
    op.drop_constraint(
        "ck_in_scope_assessments_topic_not_empty",
        "in_scope_assessments",
        type_="check",
    )
    op.drop_column("in_scope_assessments", "topic")


def downgrade() -> None:
    # 過去データは復元されない (production rollback 非サポート)
    op.add_column(
        "in_scope_assessments",
        sa.Column(
            "topic",
            sa.String(length=100),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.alter_column("in_scope_assessments", "topic", server_default=None)
    op.create_check_constraint(
        "ck_in_scope_assessments_topic_not_empty",
        "in_scope_assessments",
        "topic <> ''",
    )
    op.create_check_constraint(
        "ck_in_scope_assessments_topic_format",
        "in_scope_assessments",
        "topic ~ '^[a-z0-9]+( [a-z0-9]+){0,2}$'",
    )
