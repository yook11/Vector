"""merge briefing (w2) and trends (r5) migration heads

briefing chapters (``w2_briefings_summary_chapters``) と trends generated_at
(``r5_drop_trends_gen_default``) はいずれも ``r4_rename_trends_snapshots`` を
親に持つ独立した head として並んでいた。両 feature を統合した結果 head が 2 つに
分岐するため、DDL を持たない merge revision で単一 head に畳む。

schema は変更しない (純粋な DAG 統合)。

Revision ID: w3_merge_briefing_trends
Revises: w2_briefings_summary_chapters, r5_drop_trends_gen_default
Create Date: 2026-06-08 14:17:40.844591

"""

from collections.abc import Sequence

# migration_gate: DDL を持たない merge revision のため expand。
MIGRATION_KIND = "expand"

revision: str = "w3_merge_briefing_trends"
down_revision: tuple[str, ...] = (
    "w2_briefings_summary_chapters",
    "r5_drop_trends_gen_default",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
