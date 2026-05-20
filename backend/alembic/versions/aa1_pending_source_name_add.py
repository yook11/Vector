"""add pending_html_articles.source_name (nullable, step 3a).

PendingHtmlArticle の source identity を表層列に昇格する 3 段 migration の
**Step 3a**。domain identity (`source_name`) は現在 `staged_attributes` JSONB
の中に押し込められており、`source_id` (infra identity) との整合保証も無い
(spec ``Pending source identity refactor.md`` #1/#2 の倒立)。

Step 3a は **nullable で列を追加するだけ** の forward-only 変更で、既存
全行は NULL のまま。3b (backfill) → 3c (NOT NULL + composite FK) と続く
3 step の最初。中間状態で test が落ちないよう、3a 単独で適用しても既存
書き込み経路 (新規 pending 行作成) が壊れないこと (= source_name 列が
nullable) が肝。

不変条件 (Step 3a 後の状態):
- ``pending_html_articles.source_name`` 列が ``VARCHAR(50)`` で存在
- 列は nullable
- 既存全行で ``source_name IS NULL`` (backfill は 3b で行う)

Revision ID: aa1_pending_source_name_add
Revises: a3_drop_assessment_topic
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa1_pending_source_name_add"
down_revision: str | None = "a3_drop_assessment_topic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # deploy window 内で他 tx が長く lock を握る事故を予防 (recent pattern 整合)。
    op.execute("SET lock_timeout = '5s';")

    # nullable で列を追加するだけ。NOT NULL / composite FK は 3c で張る。
    # 物理表現は ``SourceNameType.impl = String(50)`` ([types.py:66]) と整合。
    op.add_column(
        "pending_html_articles",
        sa.Column("source_name", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pending_html_articles", "source_name")
