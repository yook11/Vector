"""tighten out_of_scope_assessments snapshot columns to NOT NULL + CHECK.

Stage 4 Assessment 永続化の対称化 PR2: PR1 (``w1_oos_snapshot_columns``) で
``out_of_scope_assessments`` に nullable で追加した ``translated_title`` /
``summary`` を NOT NULL 化し、空文字禁止の CHECK 制約を ``in_scope_assessments``
側 (``ck_in_scope_assessments_translated_title_not_empty`` /
``ck_in_scope_assessments_summary_not_empty``) と対称に張る。

PR1 merge 後 / PR2 デプロイ前の窓で Repository.save_out_of_scope が新列を渡さない
状態で INSERT した行が NULL を抱えたまま残るケースがあり (memory:
``feedback_worker_restart_after_orm_change`` の典型例)、本 migration はそれ単体で
冪等に締められるよう upgrade() 冒頭に 2 段の safety-net を持つ:

1. ``article_extractions`` から再 backfill (extraction 側は NOT NULL なので、
   FK 経由で snapshot 値を復元できる)
2. それでも NULL が残る孤立行は削除 (CASCADE FK + extraction 側 NOT NULL 前提
   で通常は 0 行。最後の保険)

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
    # Safety net 1: PR1 merge 後 / PR2 デプロイ前の窓で snapshot 列が埋まらずに
    # INSERT された行を再 backfill する。extraction 側は NOT NULL なので、FK で
    # 結合できる行は必ず値を取り戻せる。
    op.execute(
        sa.text(
            """
            UPDATE out_of_scope_assessments osa
            SET
                translated_title = ae.translated_title,
                summary = ae.summary
            FROM article_extractions ae
            WHERE osa.extraction_id = ae.id
              AND (osa.translated_title IS NULL OR osa.summary IS NULL);
            """
        )
    )
    # Safety net 2: extraction が CASCADE で消える前の race 等で取り残された
    # 孤立行は削除する。CASCADE FK + extraction.translated_title NOT NULL のため
    # 通常は 0 行だが、本番でのデータ起因停止を防ぐ最後の保険。
    op.execute(
        sa.text(
            """
            DELETE FROM out_of_scope_assessments
            WHERE translated_title IS NULL OR summary IS NULL;
            """
        )
    )
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
