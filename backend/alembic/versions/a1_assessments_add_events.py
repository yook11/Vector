"""add events JSONB column to in/out_of_scope_assessments.

event-extraction PR 1: AI が「何が起きたか (description) + 登場した固有名
(mentions)」のペア配列 ``events`` を出力するようにする並列運用 column。
既存 ``topic`` カラムは PR 4 で削除予定。

- ``in_scope_assessments.events JSONB NULL`` を追加
- ``out_of_scope_assessments.events JSONB NULL`` を追加
  (InScope と対称、out-of-scope と判定された記事の events も検証目的で保持)

NULL 許容: 既存行は NULL のまま、新規行のみ実値 ([] or values) を持つ。
NULL = PR 1 デプロイ前、[] = AI が events を返さなかった、values = 抽出済み。
CHECK 制約 / index は PR 3 で集計要件が固まってから別途追加検討する。

Revision ID: a1_assessments_add_events
Revises: z3_drop_extraction_ai_model
Create Date: 2026-05-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a1_assessments_add_events"
down_revision: str | None = "z3_drop_extraction_ai_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column(
        "in_scope_assessments",
        sa.Column("events", JSONB(), nullable=True),
    )
    op.add_column(
        "out_of_scope_assessments",
        sa.Column("events", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("out_of_scope_assessments", "events")
    op.drop_column("in_scope_assessments", "events")
