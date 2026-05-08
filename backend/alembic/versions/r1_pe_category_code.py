"""add nullable category and code columns to pipeline_events.

PR3.5-b: error taxonomy の Layer 1 dispatch 軸 (category) と Layer 2 業務識別子
(code) を pipeline_events に焼付するための列追加。

設計範囲:
- Layer1Category は **article-bound analysis stages** (extraction / classification /
  embedding) 専用の処理結果分類。dispatch / source_fetch / content_fetch では値を
  持たない (NULL のまま)。よって列は nullable + 値域 CHECK のみ。
- code は Layer 2 エラー型の CODE ClassVar (例 'ai_error_input_rejected') を入れる
  自由形式列。CHECK 制約なし (型が増えても migration を書き換えない)。

詳細: ``specs/pipeline-events-error-taxonomy.md``

Revision ID: r1_pe_category_code
Revises: s3_drop_article_urls
Create Date: 2026-05-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "r1_pe_category_code"
down_revision: str | None = "s3_drop_article_urls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CATEGORY_VALUES = (
    "success",
    "idempotent_skip",
    "retryable",
    "non_retryable_drop_article",
    "non_retryable_keep_article",
    "unknown",
)


def upgrade() -> None:
    # category: nullable。article-bound analysis stages のみ値を入れる。
    # 既存行は NULL のまま放置 (CHECK は IS NULL を許容)。
    op.add_column(
        "pipeline_events",
        sa.Column("category", sa.String(length=40), nullable=True),
    )

    # code: nullable、CHECK なし (PR3.5-c 以降で値が入り始める)
    op.add_column(
        "pipeline_events",
        sa.Column("code", sa.String(length=60), nullable=True),
    )

    # 値域 CHECK: NULL も許容
    values_sql = ",".join(f"'{v}'" for v in _CATEGORY_VALUES)
    op.create_check_constraint(
        "ck_pipeline_events_category",
        "pipeline_events",
        f"category IS NULL OR category IN ({values_sql})",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pipeline_events_category", "pipeline_events", type_="check"
    )
    op.drop_column("pipeline_events", "code")
    op.drop_column("pipeline_events", "category")
