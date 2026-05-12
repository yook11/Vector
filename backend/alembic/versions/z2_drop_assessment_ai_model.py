"""drop assessment ai_model columns (audit-only).

`in_scope_assessments.ai_model` / `out_of_scope_assessments.ai_model` カラムは
「業務行に焼かれていたが業務クエリで読まれていない」書き込み専用カラムで、
同値が ``pipeline_events.payload.ai_model`` (audit) にも記録されている
二重保存だった。Pure DI 前提 (composition root で assessor 1 つだけ配線) では
「行ごとの model 名」は無意味なので、業務行から監査属性を抜き
``pipeline_events`` を SSoT 化する (feedback_outcome_purification)。

Stage 5 (`z1_drop_embedding_model_column`) の前例と同型の refactor。

- ``ck_in_scope_assessments_ai_model_not_empty`` を drop
- ``in_scope_assessments.ai_model`` カラムを drop
- ``ck_out_of_scope_assessments_ai_model_not_empty`` を drop
- ``out_of_scope_assessments.ai_model`` カラムを drop

forward-only。downgrade すると過去の業務行 ``ai_model`` 値は失われる
(audit 経由で復元可能なため downgrade 自体を非サポートとする)。

Revision ID: z2_drop_assessment_ai_model
Revises: z1_drop_embedding_model_column
Create Date: 2026-05-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z2_drop_assessment_ai_model"
down_revision: str | None = "z1_drop_embedding_model_column"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # deploy window 内で他 tx が長く lock を握る事故を予防 (recent pattern と整合)。
    op.execute("SET lock_timeout = '5s';")

    op.drop_constraint(
        "ck_in_scope_assessments_ai_model_not_empty",
        "in_scope_assessments",
        type_="check",
    )
    op.drop_column("in_scope_assessments", "ai_model")

    op.drop_constraint(
        "ck_out_of_scope_assessments_ai_model_not_empty",
        "out_of_scope_assessments",
        type_="check",
    )
    op.drop_column("out_of_scope_assessments", "ai_model")


def downgrade() -> None:
    raise NotImplementedError(
        "forward-only: column data is discarded; "
        "see pipeline_events.payload.ai_model audit for model history"
    )
