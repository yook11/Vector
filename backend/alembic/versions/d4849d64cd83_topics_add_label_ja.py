"""topics_add_label_ja

トピックの表示用日本語ラベル列を追加する。シードトピックには手動キュレーションで
ラベルを付与し、AI 動的生成トピックは NULL でフロント側英語フォールバック表示。

Revision ID: d4849d64cd83
Revises: 640eb6c829eb
Create Date: 2026-04-23 05:31:21.387483

"""

import sqlalchemy as sa
from alembic import op

revision: str = "d4849d64cd83"
down_revision: str | None = "640eb6c829eb"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.add_column(
        "topics",
        sa.Column("label_ja", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("topics", "label_ja")
