"""add_other_category

Stage 2 分類カテゴリに `other` (表示名「市場・規制」) を 12 番目として追加する。
先端技術 11 カテゴリに該当しないが投資判断に寄与する記事 (規制・政策動向・
マクロ経済・金融政策・地政学・国際情勢・市場動向・コモディティ等) を吸収する。

設計 SSoT: specs/signal-noise-filter.md (D9, D23)。`other` は Step 2 の独立分岐
として CLASSIFICATION_PROMPT に追加され、Step 1 の 11 カテゴリ判定で fall through
した「投資関連だがカテゴリ未満」を救う。slug 形式は既存 categories の正規表現
``^[a-z0-9][a-z0-9_]{0,49}$`` に適合する。

Revision ID: o11_add_other_category
Revises: o10_add_meta_ai
Create Date: 2026-05-04

"""

from collections.abc import Sequence

from alembic import op

revision: str = "o11_add_other_category"
down_revision: str | None = "o10_add_meta_ai"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("INSERT INTO categories (slug, name) VALUES ('other', '市場・規制')")


def downgrade() -> None:
    op.execute("DELETE FROM categories WHERE slug = 'other'")
