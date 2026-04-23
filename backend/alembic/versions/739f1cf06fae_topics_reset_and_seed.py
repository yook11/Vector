"""topics_reset_and_seed

既存の article_analyses / article_rejections / topics を全削除し、業界視点・主題中心の
シード 25 件を投入する。完全リセットのため downgrade では復元しない（label_ja を持つ
シード行のみ削除）。

Revision ID: 739f1cf06fae
Revises: d4849d64cd83
Create Date: 2026-04-23 05:35:24.813491

"""

import sqlalchemy as sa
from alembic import op

revision: str = "739f1cf06fae"
down_revision: str | None = "d4849d64cd83"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


SEED_TOPICS: list[dict[str, str]] = [
    {"name": "llm",                       "label_ja": "大規模言語モデル",        "category_slug": "ai"},
    {"name": "ai agents",                 "label_ja": "AIエージェント",          "category_slug": "ai"},
    {"name": "quantum computing",         "label_ja": "量子コンピューティング",  "category_slug": "computing"},
    {"name": "cell therapy",              "label_ja": "細胞治療",                "category_slug": "bio"},
    {"name": "gene therapy",              "label_ja": "遺伝子治療",              "category_slug": "bio"},
    {"name": "mrna platforms",            "label_ja": "mRNAプラットフォーム",    "category_slug": "bio"},
    {"name": "lithography",               "label_ja": "リソグラフィ",            "category_slug": "semiconductor"},
    {"name": "memory",                    "label_ja": "半導体メモリ",            "category_slug": "semiconductor"},
    {"name": "nuclear fusion",            "label_ja": "核融合",                  "category_slug": "energy"},
    {"name": "small modular reactor",     "label_ja": "小型モジュール炉（SMR）", "category_slug": "energy"},
    {"name": "superconductors",           "label_ja": "超伝導体",                "category_slug": "materials"},
    {"name": "additive manufacturing",    "label_ja": "アディティブ製造",        "category_slug": "materials"},
    {"name": "6g",                        "label_ja": "6G",                      "category_slug": "network"},
    {"name": "open ran",                  "label_ja": "Open RAN",                "category_slug": "network"},
    {"name": "satellite internet",        "label_ja": "衛星インターネット",      "category_slug": "network"},
    {"name": "post-quantum cryptography", "label_ja": "耐量子暗号",              "category_slug": "security"},
    {"name": "ai security",               "label_ja": "AIセキュリティ",          "category_slug": "security"},
    {"name": "launch vehicles",           "label_ja": "ロケット",                "category_slug": "space"},
    {"name": "satellite constellations",  "label_ja": "衛星コンステレーション",  "category_slug": "space"},
    {"name": "lunar program",             "label_ja": "月面プログラム",          "category_slug": "space"},
    {"name": "mars exploration",          "label_ja": "火星探査",                "category_slug": "space"},
    {"name": "humanoid robots",           "label_ja": "ヒューマノイドロボット",  "category_slug": "robotics"},
    {"name": "autonomous driving",        "label_ja": "自動運転",                "category_slug": "robotics"},
    {"name": "drones",                    "label_ja": "ドローン",                "category_slug": "robotics"},
    {"name": "evtol",                     "label_ja": "eVTOL",                   "category_slug": "robotics"},
]


def upgrade() -> None:
    # FK RESTRICT のため analyses/rejections を先に削除してから topics を消す
    op.execute("DELETE FROM article_analyses;")
    op.execute("DELETE FROM article_rejections;")
    op.execute("DELETE FROM topics;")

    bind = op.get_bind()
    cat_rows = bind.execute(sa.text("SELECT id, slug FROM categories")).fetchall()
    cat_id_by_slug = {row.slug: row.id for row in cat_rows}

    missing = sorted(
        {seed["category_slug"] for seed in SEED_TOPICS} - cat_id_by_slug.keys()
    )
    if missing:
        raise RuntimeError(f"Missing category slugs in DB: {missing}")

    for seed in SEED_TOPICS:
        bind.execute(
            sa.text(
                "INSERT INTO topics (name, category_id, label_ja, created_at) "
                "VALUES (:name, :cat_id, :label_ja, now())"
            ),
            {
                "name": seed["name"],
                "cat_id": cat_id_by_slug[seed["category_slug"]],
                "label_ja": seed["label_ja"],
            },
        )


def downgrade() -> None:
    # 完全リセットのため元データの復元は不可。シード行のみ削除する。
    op.execute("DELETE FROM topics WHERE label_ja IS NOT NULL;")
