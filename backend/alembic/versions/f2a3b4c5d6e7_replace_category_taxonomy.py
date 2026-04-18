"""カテゴリ体系を再設計 — 旧10カテゴリを新10カテゴリに置き換え。

fintech 廃止・security 新設。既存 topics は前段マイグレーションで
TRUNCATE 済みのため単純な DELETE → INSERT で置換。

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None

NEW_CATEGORIES = [
    (1, "ai", "AI"),
    (2, "robotics", "ロボティクス・モビリティ"),
    (3, "semiconductor", "半導体"),
    (4, "computing", "次世代コンピューティング"),
    (5, "network", "次世代ネットワーク"),
    (6, "security", "セキュリティ"),
    (7, "space", "宇宙"),
    (8, "bio", "ゲノム・バイオ"),
    (9, "materials", "新素材・先進製造"),
    (10, "energy", "次世代エネルギー"),
]

OLD_CATEGORIES = [
    (1, "ai_ml", "AI・ML"),
    (2, "biotech", "バイオテック"),
    (3, "energy", "エネルギー"),
    (4, "fintech", "フィンテック"),
    (5, "materials", "素材科学"),
    (6, "quantum", "量子コンピュータ"),
    (7, "robotics", "ロボティクス"),
    (8, "semiconductor", "半導体"),
    (9, "space", "宇宙"),
    (10, "telecom", "通信"),
]


def upgrade() -> None:
    # topics は前段の e1f2a3b4c5d6 で TRUNCATE 済みだが、念のため削除
    op.execute("DELETE FROM topics")
    op.execute("DELETE FROM categories")

    # sequence リセット
    conn = op.get_bind()
    seq_name = conn.execute(
        sa.text("SELECT pg_get_serial_sequence('categories', 'id')")
    ).scalar_one()

    for cat_id, slug, name in NEW_CATEGORIES:
        op.execute(
            sa.text(
                "INSERT INTO categories (id, slug, name) VALUES (:id, :slug, :name)"
            ).bindparams(id=cat_id, slug=slug, name=name)
        )

    op.execute(
        sa.text(f"SELECT setval('{seq_name}', :val)").bindparams(
            val=len(NEW_CATEGORIES) + 1
        )
    )


def downgrade() -> None:
    op.execute("DELETE FROM topics")
    op.execute("DELETE FROM categories")

    conn = op.get_bind()
    seq_name = conn.execute(
        sa.text("SELECT pg_get_serial_sequence('categories', 'id')")
    ).scalar_one()

    for cat_id, slug, name in OLD_CATEGORIES:
        op.execute(
            sa.text(
                "INSERT INTO categories (id, slug, name) VALUES (:id, :slug, :name)"
            ).bindparams(id=cat_id, slug=slug, name=name)
        )

    op.execute(
        sa.text(f"SELECT setval('{seq_name}', :val)").bindparams(
            val=len(OLD_CATEGORIES) + 1
        )
    )
