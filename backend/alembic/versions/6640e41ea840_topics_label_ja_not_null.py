"""topics_label_ja_not_null

再分析完了を前提に label_ja に NOT NULL 制約を付与する。シードと AI 動的生成の
両方が label_ja を必ず持つ設計のため、本マイグレーションは Phase 8 の再分析
ワンショット実行の完了後に適用する。

Revision ID: 6640e41ea840
Revises: 739f1cf06fae
Create Date: 2026-04-23 06:02:21.873375

"""

from alembic import op

revision: str = "6640e41ea840"
down_revision: str | None = "739f1cf06fae"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.alter_column("topics", "label_ja", nullable=False)


def downgrade() -> None:
    op.alter_column("topics", "label_ja", nullable=True)
