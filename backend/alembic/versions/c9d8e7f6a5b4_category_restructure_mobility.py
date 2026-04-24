"""category_restructure_mobility

カテゴリ体系を v3 として再構成する:

- mobility カテゴリを新設 (11 個目)
- robotics の日本語ラベルを narrow 化後の表記「ロボティクス」に更新
  (旧: ロボティクス・モビリティ)
- materials の日本語ラベルを「素材・マテリアルインフォマティクス」に更新
  (旧: 新素材・先進製造)
- 「人・物を運ぶ機体」に属する topics (autonomous driving / drones / evtol) を
  robotics から mobility に付け替える (humanoid robots は robotics に残留)

slug は全て不変のため、API / フロント層は既存クエリが自動追従する。
article_analyses / article_rejections は topic_id 参照のまま変更不要
(topic -> category の category_id 付け替えで自動的にロールアップ先が変わる)。

Revision ID: c9d8e7f6a5b4
Revises: b2934b631768
Create Date: 2026-04-24

"""

from alembic import op

revision: str = "c9d8e7f6a5b4"
down_revision: str | None = "b2934b631768"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    # 1) mobility カテゴリを新設
    op.execute("INSERT INTO categories (slug, name) VALUES ('mobility', 'モビリティ')")

    # 2) 既存カテゴリの日本語ラベルを narrow 化後の表記に更新
    op.execute("UPDATE categories SET name = 'ロボティクス' WHERE slug = 'robotics'")
    op.execute(
        "UPDATE categories SET name = '素材・マテリアルインフォマティクス' "
        "WHERE slug = 'materials'"
    )

    # 3) 「人・物を運ぶ機体」トピックを mobility に付け替え
    op.execute(
        "UPDATE topics "
        "SET category_id = (SELECT id FROM categories WHERE slug = 'mobility') "
        "WHERE name IN ('autonomous driving', 'drones', 'evtol')"
    )


def downgrade() -> None:
    # 1) トピックを robotics に戻す
    op.execute(
        "UPDATE topics "
        "SET category_id = (SELECT id FROM categories WHERE slug = 'robotics') "
        "WHERE name IN ('autonomous driving', 'drones', 'evtol')"
    )

    # 2) 日本語ラベルを v2 の表記に戻す
    op.execute(
        "UPDATE categories SET name = '新素材・先進製造' WHERE slug = 'materials'"
    )
    op.execute(
        "UPDATE categories SET name = 'ロボティクス・モビリティ' "
        "WHERE slug = 'robotics'"
    )

    # 3) mobility カテゴリを削除
    op.execute("DELETE FROM categories WHERE slug = 'mobility'")
