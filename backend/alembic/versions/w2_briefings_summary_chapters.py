"""replace weekly_briefings.overview with summary + chapters (clean break).

briefing 本文構造の作り替え: 単一長文 ``overview`` (max 8,000字) を廃止し、
``summary`` (今週の総括リード) と ``chapters`` (章 = heading 見出し + body 本文の
リスト) に置換する。headline 直後に総括を置き、本文を章立てしたストーリーとして
構造化するため。

clean break:
    旧 overview (単一 Text) と新 summary / chapters (Text + JSONB) は非互換。
    briefing は週次で再生成されるため、移行コードを書くより clean break で既存行を
    全削除する方が安全。downgrade も同様に既存行を全削除してから overview を再追加
    する (新 schema で再生成済みの行があると NOT NULL overview を追加できないため)。

deploy 段取り (順序が安全性に直結):
    旧 backend は ``WeeklyBriefing.overview`` を毎 select するため、overview 列を
    落とした瞬間に旧 backend の詳細が壊れる。また新 backend が summary/chapters を
    返す一方で旧 frontend zod は overview を期待するため、新 shape が出た瞬間に
    旧 frontend 詳細が 500 になる。よって stop-first + 再生成を最後にする
    (短時間のメンテナンス窓を許容):
    1. 旧 backend(api) / worker-briefing / scheduler-briefing を停止
    2. 本 migration を head まで進め、全行が消えていることを確認
    3. 新 backend を deploy / 起動 (この時点でテーブルは空)
    4. 新 frontend を deploy
    5. CLI ``app.insights.briefing.cli.generate_briefing`` を全カテゴリで実行し
       新 schema で再生成
    空テーブル窓は安全: 詳細は EmptyBriefing (shape 不変) を返し、一覧は headline
    のみ使うため新旧どちらの frontend でも壊れない。

Revision ID: w2_briefings_summary_chapters
Revises: r4_rename_trends_snapshots
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "w2_briefings_summary_chapters"
down_revision: str | None = "r4_rename_trends_snapshots"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None

# migration_gate: DELETE + drop_column + server_default 無しの NOT NULL 追加を含むため contract。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    # clean break: 旧 overview 単一長文は新 summary / chapters と非互換。
    # 既存 briefing を全削除してから列を入れ替える。
    op.execute("DELETE FROM weekly_briefings;")
    op.drop_column("weekly_briefings", "overview")
    op.add_column(
        "weekly_briefings",
        sa.Column("summary", sa.Text(), nullable=False),
    )
    op.add_column(
        "weekly_briefings",
        sa.Column("chapters", JSONB(astext_type=sa.Text()), nullable=False),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    # clean break を双方向化: 新 schema で再生成済みの行があると NOT NULL の
    # overview を追加できないため、先に全行削除する。
    op.execute("DELETE FROM weekly_briefings;")
    op.drop_column("weekly_briefings", "chapters")
    op.drop_column("weekly_briefings", "summary")
    op.add_column(
        "weekly_briefings",
        sa.Column("overview", sa.Text(), nullable=False),
    )
