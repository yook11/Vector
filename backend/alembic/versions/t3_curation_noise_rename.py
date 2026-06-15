"""rename curation_noises columns + widen pipeline_events.outcome_code.

Tier 3 (curation-01 / completion-01):

1. ``curation_noises.title_ja`` / ``summary_ja`` を signal 側 ``article_curations``
   と語彙を揃え ``translated_title`` / ``summary`` に rename する。AI/SDK 境界の
   ``title_ja`` / ``summary_ja`` は据え置き (repository が persistence 列へ写す)。
   列 rename で CHECK 式の列参照は postgres が自動追従し、constraint 名は別途
   RENAME する。排他 trigger は ``analyzable_article_id`` を参照し title/summary を
   参照しないため無影響。data rewrite なし。

2. completion-01 の新 ready-build outcome code
   ``completion_ready_build_blocked_incomplete_article_not_running`` が 61 字で
   既存 ``pipeline_events.outcome_code`` ``varchar(60)`` を超えるため、列幅を
   ``varchar(80)`` に広げる (varchar 拡幅は postgres では metadata-only)。

deploy は stop-the-world (rolling だと旧 code が新列名を参照できない)。

Revision ID: t3_curation_noise_rename
Revises: x6_analyzed_article_ids
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t3_curation_noise_rename"
down_revision: str | None = "x6_analyzed_article_ids"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    # rename は metadata 操作だが table lock を取るため、本番で長時間 lock が
    # 取れない場合は早期 fail させる (先例は t2_curation_table_rename)。
    op.execute("SET lock_timeout = '5s';")

    op.alter_column("curation_noises", "title_ja", new_column_name="translated_title")
    op.alter_column("curation_noises", "summary_ja", new_column_name="summary")

    op.execute(
        "ALTER TABLE curation_noises RENAME CONSTRAINT "
        "ck_curation_noises_title_ja_not_empty "
        "TO ck_curation_noises_translated_title_not_empty;"
    )
    op.execute(
        "ALTER TABLE curation_noises RENAME CONSTRAINT "
        "ck_curation_noises_summary_ja_not_empty "
        "TO ck_curation_noises_summary_not_empty;"
    )

    op.alter_column(
        "pipeline_events",
        "outcome_code",
        type_=sa.String(80),
        existing_type=sa.String(60),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    op.alter_column(
        "pipeline_events",
        "outcome_code",
        type_=sa.String(60),
        existing_type=sa.String(80),
        existing_nullable=False,
    )

    op.execute(
        "ALTER TABLE curation_noises RENAME CONSTRAINT "
        "ck_curation_noises_translated_title_not_empty "
        "TO ck_curation_noises_title_ja_not_empty;"
    )
    op.execute(
        "ALTER TABLE curation_noises RENAME CONSTRAINT "
        "ck_curation_noises_summary_not_empty "
        "TO ck_curation_noises_summary_ja_not_empty;"
    )

    op.alter_column("curation_noises", "translated_title", new_column_name="title_ja")
    op.alter_column("curation_noises", "summary", new_column_name="summary_ja")
