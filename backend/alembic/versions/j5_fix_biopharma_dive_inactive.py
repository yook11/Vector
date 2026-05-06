"""BioPharma Dive の is_active=true 不整合を修正する。

Phase 1a (commit de82e47, 2026-04-20 "feat(collection): expand news sources from
9 to 12 active") で deactivate されるべき 5 ソースのうち、BioPharma Dive のみ
反映漏れしていた。Fetcher 実装は削除済み・registry にも未登録のため、
scheduler が dispatch すると registry KeyError で失敗し続けていた。

Fetcher 再実装ではなく `is_active=false` に揃えることで整合性を取り戻す。
将来再有効化する場合は本リファクタリング (collection-acquisition-redesign)
完了後に別途 Fetcher を実装する。

Revision ID: j5_fix_biopharma_dive_inactive
Revises: g3c4d5e6f8a9
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op

revision = "j5_fix_biopharma_dive_inactive"
# PR4 (red-team C2 防御) で i4_seed_e2e_users を migration から削除し、
# E2E seed は backend/scripts/seed_e2e_users.py に分離。本 migration は
# i4 の前駆 g3c4d5e6f8a9 に直接接続する。
down_revision = "g3c4d5e6f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE news_sources SET is_active = false, updated_at = now() "
        "WHERE name = 'BioPharma Dive'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE news_sources SET is_active = true, updated_at = now() "
        "WHERE name = 'BioPharma Dive'"
    )
