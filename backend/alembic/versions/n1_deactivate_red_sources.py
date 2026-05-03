"""本番公開前に RED 判定 12 ソースを is_active=false に落とす。

ToS / robots.txt 上で商用 AI 翻訳・配信が認められないと判定された 12 ソースを
無効化する。Fetcher 実装と FETCHERS dict 登録は維持し、将来個別許可が得られた
場合に再有効化できる構造を残す (`alembic downgrade -1` または個別 UPDATE)。

判定根拠と代替ソース計画: specs/source-strategy/production-readiness-2026-05-03.md

Revision ID: n1_deactivate_red_sources
Revises: m1_briefings_create
Create Date: 2026-05-03
"""

from __future__ import annotations

from alembic import op

revision = "n1_deactivate_red_sources"
down_revision = "m1_briefings_create"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE news_sources
        SET is_active = false, updated_at = now()
        WHERE name IN (
            'TechCrunch',
            'Engadget',
            'The Register',
            'IEEE Spectrum',
            'ITmedia NEWS',
            'ITmedia AI+',
            'EE Times Japan',
            'MONOist',
            'Microsoft Research',
            'The Quantum Insider',
            'SpaceNews',
            'FierceBiotech'
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE news_sources
        SET is_active = true, updated_at = now()
        WHERE name IN (
            'TechCrunch',
            'Engadget',
            'The Register',
            'IEEE Spectrum',
            'ITmedia NEWS',
            'ITmedia AI+',
            'EE Times Japan',
            'MONOist',
            'Microsoft Research',
            'The Quantum Insider',
            'SpaceNews',
            'FierceBiotech'
        )
        """
    )
