"""drop server_default on trends_snapshots.generated_at

snapshot を「API レスポンスそのもの」にして verbatim 配信する設計に伴い、
``generated_at`` は生成側 (Service) がアプリで 1 つ確定し、JSON payload と DB 列の
双方へ同値を入れる。時計の源をアプリ 1 つに統一するため、DB の server_default
(``now()``) を撤去する。列は NOT NULL のまま (生成側が常に明示供給する)。

contract: server_default 撤去後は ``generated_at`` を渡さない INSERT が NOT NULL
違反になる。常に明示供給する新コードを先に live させてから適用すること。

Revision ID: r5_drop_trends_gen_default
Revises: r4_rename_trends_snapshots
Create Date: 2026-06-08 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "r5_drop_trends_gen_default"
down_revision: str | None = "r4_rename_trends_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: server_default 撤去で旧 INSERT が壊れるため contract。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.alter_column(
        "trends_snapshots",
        "generated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=None,
    )


def downgrade() -> None:
    op.alter_column(
        "trends_snapshots",
        "generated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=sa.func.now(),
    )
