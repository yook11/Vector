# Topic Tagging — データモデル設計

## 新規: topics テーブル

```sql
CREATE TABLE topics (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (name, category_id)
);

CREATE INDEX ix_topics_category_id ON topics (category_id);
```

### カラム定義

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | SERIAL | PK | 自動採番 |
| `name` | VARCHAR(100) | NOT NULL, UNIQUE(name, category_id) | 正規化済み Topic ラベル |
| `category_id` | INTEGER | FK RESTRICT, NOT NULL, INDEX | 所属カテゴリ |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | 作成日時 |

### 設計判断

| 判断 | 理由 |
|---|---|
| `(name, category_id)` で UNIQUE | 同名でも別 Category なら別 Topic。フィルタが Category 内で閉じる |
| `name` 単体の UNIQUE なし | 異なる Category に同名 Topic が存在しうる (例: "supply chain disruption") |
| `updated_at` なし | Topic 名は変更しない。修正が必要なら新 Topic を作ってマージ |
| `ON DELETE RESTRICT` | Category 削除時に所属 Topic があれば拒否 |

## 変更: article_analyses テーブル

```sql
ALTER TABLE article_analyses
    ADD COLUMN topic_id INTEGER REFERENCES topics(id) ON DELETE RESTRICT;

CREATE INDEX ix_article_analyses_topic_id ON article_analyses (topic_id);
```

### 設計判断

| 判断 | 理由 |
|---|---|
| `ON DELETE RESTRICT` | Topic 削除時に参照している記事があれば拒否。マージ操作は「付け替え → 削除」の2ステップを Service で強制する |
| 初期 NULL 許可 | 既存 article_analyses にデータがあるため。移行完了後に NOT NULL 化 |
| INDEX 追加 | topic でのフィルタクエリに必要 |

## 削除予定: 旧テーブル

Topic 移行完了後に削除する:

| テーブル | 理由 |
|---|---|
| `keywords` | Topic に置き換え |
| `article_keywords` | M:N 中間テーブル不要 (1:1 の FK に変更) |

## TopicName 値オブジェクト

```python
class TopicName(RootModel[str]):
    """Topic ラベル。正規化済みの英語小文字。"""
    root: Annotated[
        str,
        StringConstraints(
            min_length=2,
            max_length=100,
            pattern=r"^[a-z0-9][a-z0-9 -]*[a-z0-9]$",
        ),
    ]
```

- 先頭・末尾は英数字
- 中間はスペースとハイフンも許可
- 小文字のみ (正規化済みの値が入る前提)
- 例: "ai drug discovery", "semiconductor trade policy", "6g network"

### KeywordName との違い

| | KeywordName | TopicName |
|---|---|---|
| 大文字 | 許可 | 不可 (小文字のみ) |
| 記号 (`/`, `+`, `#`, `.`, `&`) | 許可 (C++, AI/ML 等) | 不可 |
| 最小長 | 1文字 | 2文字 |
| 用途 | 人間定義のタグ名 | AI 生成 + 正規化済みラベル |

## Topic モデル

```python
class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("name", "category_id", name="uq_topics_name_category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[TopicName] = mapped_column(String(100))
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    category: Mapped[Category] = relationship(back_populates="topics")
```

- `updated_at` なし (Topic 名は不変)
- `Topic.article_analyses` 逆参照なし (集計はリポジトリのクエリで行う)

## ArticleAnalysis モデル変更

```python
class ArticleAnalysis(Base):
    # 既存フィールド省略...

    # 追加
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="RESTRICT"),
        index=True,
    )

    # 追加 (片方向リレーション)
    topic: Mapped[Topic] = relationship()
```

- 既存データを削除するため、最初から NOT NULL

## Category モデル変更

```python
class Category(Base):
    # 既存フィールド省略...

    # 追加
    topics: Mapped[list[Topic]] = relationship(back_populates="category")

    # 削除予定 (移行完了後)
    keywords: Mapped[list[Keyword]] = relationship(back_populates="category")
```

## マイグレーション計画

既存データを削除し、新しい Topic 体系でゼロから蓄積する方針。
段階的移行は不要。

| ステップ | 内容 |
|---|---|
| 1 | 既存の `article_analyses` + `news_articles` データをクリア |
| 2 | `article_keywords` テーブル削除 |
| 3 | `keywords` テーブル削除 |
| 4 | `topics` テーブル作成 |
| 5 | `article_analyses.topic_id` FK 追加 (NOT NULL) |
