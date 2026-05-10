"""add overview column to weekly_briefings (clean break wipe).

Phase 1B-γ overview restructure: WeeklyBriefingContent VO に ``overview``
field を追加し、UI / prompt 側を overview 主体構成へ再編する一連の変更
の DB 側。

clean break:
    旧 stories 構造 (title / analysis を持つ) は新 schema (takeaway のみ)
    と非互換なため、本 migration の upgrade では既存 ``weekly_briefings``
    行を全削除する。本番運用は γ-1 deploy 後 1 週間程度で過去 briefing を
    analytics 的に残す要件はないため、移行コードを書くより clean break
    する方が安全。

deploy 段取り:
    1. backend image deploy 前に本 migration を head まで進める
    2. ``weekly_briefings`` の 全行が消えていることを確認
    3. backend / worker-briefing / scheduler-briefing を新 image で再起動
    4. CLI ``app.insights.briefing.cli.generate_briefing`` を全 11 カテゴリ
       について実行し、新 schema で再生成
    5. frontend を deploy

Revision ID: v1_briefings_add_overview
Revises: u1_assessment_stage_rename
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "v1_briefings_add_overview"
down_revision: str | None = "u1_assessment_stage_rename"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    # 1. clean break: 旧 stories 構造 (title / analysis) は新 takeaway-only
    #    schema と非互換なので、既存 briefing を全削除する。
    op.execute("DELETE FROM weekly_briefings;")

    # 2. NOT NULL の overview 列を追加。DELETE 直後なので default 値不要。
    op.add_column(
        "weekly_briefings",
        sa.Column("overview", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("weekly_briefings", "overview")
