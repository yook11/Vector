"""replace weekly_briefings.stories with key_articles + watch_points (clean break).

briefing 出力構造の作り替え (v1): ``stories[]`` (記事グループ + takeaway) を
廃止し、``key_articles[]`` (記事単位の重要記事 + significance) と
``watch_points[]`` (今後の論点 statement) に置換する。

clean break:
    旧 stories 構造と新 key_articles / watch_points 構造は非互換。briefing は
    週次で再生成されるため、移行コードを書くより clean break で既存行を全削除
    する方が安全。downgrade も同様に既存行を全削除してから stories を再追加する
    (新 schema で再生成済みの行があると NOT NULL stories を追加できないため)。

deploy 段取り (順序が安全性に直結):
    旧 backend は ``WeeklyBriefing.stories`` を毎 select するため、stories 列を
    落とした瞬間に旧 backend の一覧/詳細が壊れる。また新 backend が
    keyArticles/watchPoints を返す一方で旧 frontend zod は stories を期待する
    ため、新 shape が出た瞬間に旧 frontend 詳細が 500 になる。よって stop-first
    + 再生成を最後にする (短時間のメンテナンス窓を許容):
    1. 旧 backend(api) / worker-briefing / scheduler-briefing を停止
    2. 本 migration を head まで進め、全行が消えていることを確認
    3. 新 backend を deploy / 起動 (この時点でテーブルは空)
    4. 新 frontend を deploy
    5. CLI ``app.insights.briefing.cli.generate_briefing`` を全カテゴリで実行し
       新 schema で再生成
    空テーブル窓は安全: 詳細は EmptyBriefing (shape 不変) を返し、一覧は headline
    のみ使うため新旧どちらの frontend でも壊れない。

Revision ID: w1_briefings_key_articles
Revises: z15_assessment_key_points
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "w1_briefings_key_articles"
down_revision: str | None = "z15_assessment_key_points"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None

# migration_gate: DELETE + drop_column + server_default 無しの NOT NULL 追加を含むため contract。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    # clean break: 旧 stories 構造は新 key_articles / watch_points と非互換。
    # 既存 briefing を全削除してから列を入れ替える。
    op.execute("DELETE FROM weekly_briefings;")
    op.drop_column("weekly_briefings", "stories")
    op.add_column(
        "weekly_briefings",
        sa.Column("key_articles", JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.add_column(
        "weekly_briefings",
        sa.Column("watch_points", JSONB(astext_type=sa.Text()), nullable=False),
    )


def downgrade() -> None:
    # clean break を双方向化: 新 schema で再生成済みの行があると NOT NULL の
    # stories を追加できないため、先に全行削除する。
    op.execute("DELETE FROM weekly_briefings;")
    op.drop_column("weekly_briefings", "watch_points")
    op.drop_column("weekly_briefings", "key_articles")
    op.add_column(
        "weekly_briefings",
        sa.Column("stories", JSONB(astext_type=sa.Text()), nullable=False),
    )
