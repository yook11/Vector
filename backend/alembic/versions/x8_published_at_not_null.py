"""enforce NOT NULL on analyzable_articles.published_at.

ドメイン ``AnalyzableArticle`` は ``published_at`` を必須 VO として持ち、唯一の
書込経路 ``AnalyzableArticleRepository.save`` は常に非 null を INSERT する。DB
カラムだけが ``articles`` 時代 (d1e2f3a4b5c6) から nullable のまま残り、ドメイン
不変条件に追従していなかった。本 migration で DB 制約を揃える。

NULL 行の削除は本 migration の責務外。残存 NULL があれば SET NOT NULL は失敗する
ため、upgrade 冒頭で件数を確認し RuntimeError で早期に止める。本番では deploy-prod
runbook の pre-step (計測 → 参照する weekly_briefings 先行削除 → NULL 行削除) を
実施してから適用する。

downgrade は制約を nullable へ戻すのみ。本 migration はデータを削除しないため
schema は完全に可逆。

deploy は stop-the-world 前提 (contract)。

Revision ID: x8_published_at_not_null
Revises: x7_query_embedding_cache
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "x8_published_at_not_null"
down_revision: str | None = "x7_query_embedding_cache"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None

# migration_gate: ALTER COLUMN SET NOT NULL は full table scan + ACCESS EXCLUSIVE
# lock を取るため contract。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # 残存 NULL があれば SET NOT NULL は失敗する。削除は本 migration の責務外
    # (deploy-prod pre-step)。ここでは件数を確認し、未実施を loud に止める。
    null_count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM analyzable_articles WHERE published_at IS NULL"
            )
        )
        .scalar_one()
    )
    if null_count:
        raise RuntimeError(
            f"analyzable_articles.published_at に NULL 行が {null_count} 件残存。"
            " SET NOT NULL の前に deploy-prod pre-step で NULL 行を削除すること。"
        )

    op.alter_column(
        "analyzable_articles",
        "published_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    op.alter_column(
        "analyzable_articles",
        "published_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
