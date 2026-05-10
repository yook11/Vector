"""tighten out_of_scope_assessments snapshot columns to NOT NULL + CHECK.

Stage 4 Assessment 永続化の対称化 PR2: PR1 (``w1_oos_snapshot_columns``) で
``out_of_scope_assessments`` に nullable で追加した ``translated_title`` /
``summary`` を NOT NULL 化し、空文字禁止の CHECK 制約を ``in_scope_assessments``
側 (``ck_in_scope_assessments_translated_title_not_empty`` /
``ck_in_scope_assessments_summary_not_empty``) と対称に張る。

PR1 で全行 backfill 済 + 本 PR2 で Service/Repository が新規 INSERT 時に snapshot
を渡すよう変更されたため、NOT NULL 化は安全。デプロイ手順は memory
``feedback_worker_restart_after_orm_change`` に従い alembic upgrade 後に backend
/ worker container を必ず restart すること (旧 image が新列を渡さず INSERT して
IntegrityError で死ぬ窓を最小化する)。

Revision ID: x1_oos_snapshot_strict
Revises: w1_oos_snapshot_columns
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "x1_oos_snapshot_strict"
down_revision: str | None = "w1_oos_snapshot_columns"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "out_of_scope_assessments",
        "translated_title",
        existing_type=sa.String(length=500),
        nullable=False,
    )
    op.alter_column(
        "out_of_scope_assessments",
        "summary",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_out_of_scope_assessments_translated_title_not_empty",
        "out_of_scope_assessments",
        "translated_title != ''",
    )
    op.create_check_constraint(
        "ck_out_of_scope_assessments_summary_not_empty",
        "out_of_scope_assessments",
        "summary != ''",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_out_of_scope_assessments_summary_not_empty",
        "out_of_scope_assessments",
        type_="check",
    )
    op.drop_constraint(
        "ck_out_of_scope_assessments_translated_title_not_empty",
        "out_of_scope_assessments",
        type_="check",
    )
    op.alter_column(
        "out_of_scope_assessments",
        "summary",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.alter_column(
        "out_of_scope_assessments",
        "translated_title",
        existing_type=sa.String(length=500),
        nullable=True,
    )
