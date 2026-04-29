"""Add CHECK constraint: status-approved_at invariant on keywords.

Business rule:
- OFFICIAL → approved_at IS NOT NULL
- PROVISIONAL / BLACKLISTED → approved_at IS NULL

Revision ID: c14a1b2c3d4e
Revises: c13a1b2c3d4e
Create Date: 2026-03-28
"""

from alembic import op

revision = "c14a1b2c3d4e"
down_revision = "c13a1b2c3d4e"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "ck_keywords_status_approved_at"


def upgrade() -> None:
    # fresh DB に対する `alembic upgrade head` では、f52d4ecebe6b で seed した
    # 72 keywords が c2 の `status` server_default で 'official' を取得した上で
    # approved_at=NULL のまま c14 に到達し、本制約に違反する。dev/prod 既存環境は
    # c14 適用時点で別経路でデータが整合していたと推定されるが、構造的に正しく
    # するため制約追加の直前に invariant を満たす UPDATE を入れる。後続の
    # e1f2a3b4c5d6 で keywords テーブル自体が drop されるため、本 UPDATE が
    # 設定する approved_at の寿命は短く副作用は許容範囲。
    op.execute(
        "UPDATE keywords SET approved_at = NOW() "
        "WHERE status = 'official' AND approved_at IS NULL"
    )
    op.execute(
        "UPDATE keywords SET approved_at = NULL "
        "WHERE status != 'official' AND approved_at IS NOT NULL"
    )

    op.create_check_constraint(
        CONSTRAINT_NAME,
        "keywords",
        """
        (status = 'official' AND approved_at IS NOT NULL)
        OR
        (status != 'official' AND approved_at IS NULL)
        """,
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "keywords", type_="check")
