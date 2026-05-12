"""drop extraction ai_model columns (audit-only).

`article_extractions.ai_model` / `extraction_noises.ai_model` カラムは
「業務行に焼かれていたが業務クエリで読まれていない」書き込み専用カラムで、
同値が audit (`pipeline_events.payload.ai_model`、Stage 3 は
`GeminiExtractionPrompt.MODEL` を audit 側で焼付け) にも記録されている
二重保存だった。Pure DI 前提 (composition root で extractor 1 つだけ配線) では
「行ごとの model 名」は無意味なので、業務行から監査属性を抜き
``pipeline_events`` を SSoT 化する (feedback_outcome_purification)。

Stage 4 (`z2_drop_assessment_ai_model`) / Stage 5
(`z1_drop_embedding_model_column`) の前例と同型の refactor。

- ``ck_article_extractions_ai_model_not_empty`` を drop
- ``article_extractions.ai_model`` カラムを drop
- ``ck_extraction_noises_ai_model_not_empty`` を drop
- ``extraction_noises.ai_model`` カラムを drop

forward-only。downgrade すると過去の業務行 ``ai_model`` 値は失われる
(audit 経由で復元可能なため downgrade 自体を非サポートとする)。

Revision ID: z3_drop_extraction_ai_model
Revises: z2_drop_assessment_ai_model
Create Date: 2026-05-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z3_drop_extraction_ai_model"
down_revision: str | None = "z2_drop_assessment_ai_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # deploy window 内で他 tx が長く lock を握る事故を予防 (recent pattern と整合)。
    op.execute("SET lock_timeout = '5s';")

    op.drop_constraint(
        "ck_article_extractions_ai_model_not_empty",
        "article_extractions",
        type_="check",
    )
    op.drop_column("article_extractions", "ai_model")

    op.drop_constraint(
        "ck_extraction_noises_ai_model_not_empty",
        "extraction_noises",
        type_="check",
    )
    op.drop_column("extraction_noises", "ai_model")


def downgrade() -> None:
    raise NotImplementedError(
        "forward-only: column data is discarded; "
        "see pipeline_events.payload.ai_model audit for model history"
    )
