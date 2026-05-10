"""add translated_title / summary snapshot columns to out_of_scope_assessments.

Stage 4 Assessment 永続化の対称化 PR1/2: ``in_scope_assessments`` 側が保持して
いる point-in-time snapshot (``translated_title`` / ``summary``) を
``out_of_scope_assessments`` にも追加する。本 migration は **migration-only**
で、業務コード (Entity / Repository / Service) は本 PR では変更しない。

PR1 では nullable=True で 2 列追加 + 既存行を ``article_extractions`` から
backfill する。PR1 単独 merge 時の挙動:
    - 既存行: backfill により snapshot が埋まる
    - 新規 INSERT: Repository.save が新列を渡さないため NULL が入る (許容)

NOT NULL 化と CHECK 制約 (空文字禁止) は PR2 で Entity / Repository / Service
更新と一緒に締める。

Revision ID: w1_oos_snapshot_columns
Revises: v1_briefings_add_overview
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "w1_oos_snapshot_columns"
down_revision: str | None = "v1_briefings_add_overview"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    # 1. 2 列を nullable で追加 (既存行は次の backfill で埋め、新規 INSERT は
    #    PR2 で Repository.save が値を渡すまでは NULL)。型は in_scope 側と一致。
    op.add_column(
        "out_of_scope_assessments",
        sa.Column("translated_title", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "out_of_scope_assessments",
        sa.Column("summary", sa.Text(), nullable=True),
    )

    # 2. 既存行の backfill: extraction_id FK + CASCADE により article_extractions
    #    の対応行は必ず存在する。両列とも extraction 側で NOT NULL なので NULL
    #    が混入する余地はない。
    op.execute(
        sa.text(
            """
            UPDATE out_of_scope_assessments osa
            SET
                translated_title = ae.translated_title,
                summary = ae.summary
            FROM article_extractions ae
            WHERE osa.extraction_id = ae.id;
            """
        )
    )

    # NOT NULL 化と CHECK 制約 (空文字禁止) は PR2 で Entity / Repository /
    # Service 更新と一緒に締める。PR1 単独 merge 時に新規 INSERT が IntegrityError
    # で落ちないようにするための分割。


def downgrade() -> None:
    # 値は article_extractions からの snapshot 複製のため逆 backfill 不要。
    op.drop_column("out_of_scope_assessments", "summary")
    op.drop_column("out_of_scope_assessments", "translated_title")
