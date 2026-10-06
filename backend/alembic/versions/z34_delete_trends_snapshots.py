"""古い形のトレンドのスナップショットを全件削除し、次の生成で新しい形に作り直させる。

スナップショットは公開レスポンスをそのまま保存している。古い形は上位5に入らなかった
名前の順位を持たないため、新しい形 (specs/insights/trend-representation.md) に
変換できない。

Revision ID: z34_delete_trends_snapshots
Revises: z33_lock_exclusion_parents
"""

from collections.abc import Sequence

from alembic import op

revision: str = "z34_delete_trends_snapshots"
down_revision: str | None = "z33_lock_exclusion_parents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 既存行のDELETEは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute("DELETE FROM trends_snapshots")


def downgrade() -> None:
    # 削除した古い形のスナップショットは復元できない。
    pass
