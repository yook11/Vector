# SQLModel → SQLAlchemy DeclarativeBase 移行の理由

## 背景

Vector のモデル層は SQLModel で構築されていた。SQLModel は SQLAlchemy と Pydantic を統合し、「モデル定義とスキーマ定義を一体化」することで開発効率を上げるという設計思想を持つ。

しかしプロジェクトが成長するにつれ、SQLModel の統合がもたらすメリットよりも制約の方が大きくなった。

## 移行理由

### 1. SQLAlchemy への逃げが常態化している

`ondelete`、複合インデックス、`server_default`、partial index など、実務で必要な DB 制約の多くが SQLModel の `Field()` では表現できず、`sa_column=Column(...)` で SQLAlchemy の記法に逃げている。結果として SQLModel の簡潔さは失われ、2つの書き方が混在する状態になっている。

```python
# SQLModel の Field() では ondelete を指定できない
news_source_id: int = Field(
    sa_column=Column(
        Integer,
        ForeignKey("news_sources.id", ondelete="RESTRICT", name="fk_..."),
        nullable=False,
    )
)
```

### 2. モデルとスキーマの一体化が活きていない

SQLModel の最大の売りは「モデル = スキーマ」だが、Vector ではAPI のリクエスト/レスポンスに専用の Pydantic スキーマを定義している。DB モデルをそのまま API に露出させるのはセキュリティ上もアーキテクチャ上も望ましくないため、スキーマ層は独立して存在する。つまり SQLModel の統合メリットが発揮される場面がない。

### 3. metadata の分離による Relationship 制約

SQLModel は `SQLModel.metadata` を、DeclarativeBase は `Base.metadata` を持つ。異なる metadata に属するテーブル間では ORM の Relationship が定義できない。

既に DeclarativeBase で定義した `ArticleKeyword` と SQLModel の `NewsArticle` の間で Relationship が書けず、FK 制約のみで繋いでいる（コード上で `# cross-base` とコメントされている）。混在が続く限りこの制約は解消されない。

### 4. 命名規約の不一致

SQLModel と DeclarativeBase はインデックス・制約の自動命名規約が異なる。混在状態では Alembic の autogenerate が意図しない差分を検出し続ける。`refactor/declarative-base-migration` ブランチで命名の不一致を解消したが、根本原因（2つの Base の混在）を解消しない限り再発する。

### 5. DeclarativeBase の方がドメインモデリングに適している

SQLAlchemy の `mapped_column` は制約の表現力が高く、`__table_args__` に頼らずにインデックスや CHECK 制約を定義できる。`TypeDecorator` による型変換、`hybrid_property` によるドメインロジックの埋め込みなど、ORM としての機能が充実している。

## 移行方針

- 全テーブルを一括で移行するのではなく、段階的に進める
- 移行単位はレビュー計画の Phase に沿う（独立エンティティ → コア集約ルート → 関連テーブル → 運用系）
- 各テーブルの移行時に VO / バリデーションの見直しも併せて行う
- 移行後に Alembic autogenerate で差分ゼロを確認する

## 参考: 移行前後の比較

```python
# Before: SQLModel（sa_column への逃げが多い）
class NewsArticle(SQLModel, table=True):
    __tablename__ = "news_articles"
    id: int | None = Field(default=None, primary_key=True)
    news_source_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("news_sources.id", ondelete="RESTRICT", name="fk_..."),
            nullable=False,
        )
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        ),
    )

# After: SQLAlchemy DeclarativeBase（統一された記法）
class NewsArticle(Base):
    __tablename__ = "news_articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    news_source_id: Mapped[int] = mapped_column(
        ForeignKey("news_sources.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```