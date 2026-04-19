"""discovered_articles + articles テーブル作成、データ移行、FK 入替。

NewsArticle テーブルを discovered_articles（収集記録）と
articles（分析対象）に分離する。article_analyses の FK を
news_article_id → article_id に張り替える。

Revision ID: d1e2f3a4b5c6
Revises: g4d5e6f7h8i9
Create Date: 2026-04-20
"""

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "g4d5e6f7h8i9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. discovered_articles テーブル作成
    op.create_table(
        "discovered_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "news_source_id",
            sa.Integer(),
            sa.ForeignKey("news_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("original_url", sa.String(), nullable=False),
        sa.Column("original_title", sa.String(500), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("original_url", name="uq_discovered_articles_original_url"),
        sa.CheckConstraint(
            "original_url ~ '^https?://.+'",
            name="ck_discovered_articles_url_scheme",
        ),
        sa.CheckConstraint(
            "original_title != ''",
            name="ck_discovered_articles_title_not_empty",
        ),
    )

    # 2. articles テーブル作成
    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "discovered_article_id",
            sa.Integer(),
            sa.ForeignKey("discovered_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_title", sa.String(500), nullable=False),
        sa.Column("original_content", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "discovered_article_id", name="uq_articles_discovered_article_id"
        ),
        sa.CheckConstraint(
            "original_title != ''",
            name="ck_articles_title_not_empty",
        ),
        sa.Index("idx_articles_published", "published_at"),
    )

    # 3. news_articles → discovered_articles データ移行（ID 保持）
    op.execute("""
        INSERT INTO discovered_articles (id, news_source_id, original_url, original_title, discovered_at)
        SELECT id, news_source_id, original_url, original_title, created_at
        FROM news_articles
    """)

    # 4. news_articles WHERE original_content IS NOT NULL → articles データ移行（ID 保持）
    op.execute("""
        INSERT INTO articles (id, discovered_article_id, original_title, original_content, published_at, created_at)
        SELECT id, id, original_title, original_content, published_at, created_at
        FROM news_articles
        WHERE original_content IS NOT NULL
    """)

    # 5. シーケンスリセット
    op.execute("""
        SELECT setval(
            pg_get_serial_sequence('discovered_articles', 'id'),
            COALESCE((SELECT MAX(id) FROM discovered_articles), 0)
        )
    """)
    op.execute("""
        SELECT setval(
            pg_get_serial_sequence('articles', 'id'),
            COALESCE((SELECT MAX(id) FROM articles), 0)
        )
    """)

    # 6. article_analyses に article_id カラム追加（nullable、一時的）
    op.add_column(
        "article_analyses",
        sa.Column("article_id", sa.Integer(), nullable=True),
    )

    # 7. データ移行: article_id = news_article_id
    op.execute("""
        UPDATE article_analyses
        SET article_id = news_article_id
    """)

    # 8. 孤立チェック: article_analyses.article_id が articles.id に存在しない行
    #    存在する場合、マイグレーションを中断する
    op.execute("""
        DO $$
        DECLARE
            orphan_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO orphan_count
            FROM article_analyses aa
            WHERE NOT EXISTS (SELECT 1 FROM articles a WHERE a.id = aa.article_id);

            IF orphan_count > 0 THEN
                RAISE EXCEPTION 'Found % orphaned article_analyses rows without matching articles', orphan_count;
            END IF;
        END $$;
    """)

    # 9. article_id を NOT NULL 化 + FK + UNIQUE 制約追加
    op.alter_column("article_analyses", "article_id", nullable=False)
    op.create_foreign_key(
        "fk_article_analyses_article_id",
        "article_analyses",
        "articles",
        ["article_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_article_analyses_article_id",
        "article_analyses",
        ["article_id"],
    )

    # 10. 旧カラムと制約を削除
    op.drop_constraint(
        "uq_article_analyses_news_article_id", "article_analyses", type_="unique"
    )
    op.drop_constraint(
        "fk_article_analyses_news_article_id", "article_analyses", type_="foreignkey"
    )
    op.drop_column("article_analyses", "news_article_id")


def downgrade() -> None:
    # article_analyses: article_id → news_article_id に戻す
    op.add_column(
        "article_analyses",
        sa.Column("news_article_id", sa.Integer(), nullable=True),
    )
    op.execute("UPDATE article_analyses SET news_article_id = article_id")
    op.alter_column("article_analyses", "news_article_id", nullable=False)
    op.create_foreign_key(
        "fk_article_analyses_news_article_id",
        "article_analyses",
        "news_articles",
        ["news_article_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_article_analyses_news_article_id",
        "article_analyses",
        ["news_article_id"],
    )

    # 新しい制約・カラムを削除
    op.drop_constraint("uq_article_analyses_article_id", "article_analyses", type_="unique")
    op.drop_constraint("fk_article_analyses_article_id", "article_analyses", type_="foreignkey")
    op.drop_column("article_analyses", "article_id")

    # テーブル削除
    op.drop_table("articles")
    op.drop_table("discovered_articles")
