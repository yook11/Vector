# Category/Keyword SQLAlchemy Declarative 移行プラン

> 作成日: 2026-03-28
> ブランチ: feature/better-auth
> 前提: Phase 1 DDD モデルレビュー完了（DB CHECK 制約 c14-c16 適用済み）

## 背景

値オブジェクト（CategorySlug, CategoryName, KeywordName）のバリデーションロジックを
1箇所に集約し、全層で自動適用するために、SQLAlchemy の TypeDecorator + type_annotation_map を導入する。
SQLModel ではこのパターンが実現できないため、対象モデルのみ SQLAlchemy DeclarativeBase に移行する。

## 採用方式（3層構成）

```
VO 定義層    — Pydantic RootModel[str] + frozen=True（バリデーション SSoT）
ORM 変換層   — SQLAlchemy TypeDecorator + cache_ok=True（str ↔ VO 自動変換）
models 接続層 — DeclarativeBase + type_annotation_map（グローバル登録）
```

### SQLModel で不可能だったこと

1. **カラム型にカスタム型が使えない**: `slug: CategorySlug = Field(...)` が書けない（plain str のみ）
2. **Mapped / mapped_column が使えない**: SQLAlchemy 2.0 の型ヒントシステムと非互換
3. **type_annotation_map が使えない**: VO → TypeDecorator のグローバル登録が不可能
4. **結果**: VO は schema 層でしか使えず、service 層で `data.slug.root` のような手動変換が必要だった

## 重要な制約: SQLAlchemy と SQLModel の Relationship 併用禁止

SQLModel と DeclarativeBase はそれぞれ独立した **class registry** を持つ。
metadata の共有（FK 解決用）は可能だが、registry は共有できない。

### 何が起きるか

```python
# Keyword は DeclarativeBase (Base)
class Keyword(Base):
    article_keywords: Mapped[list["ArticleKeyword"]] = relationship(...)
    # → expression 'ArticleKeyword' failed to locate a name
    #   ArticleKeyword は SQLModel の registry にいるので解決できない
```

- `ForeignKey("categories.id")` — テーブル名ベース → **metadata 内で解決 → 動く**
- `relationship("ArticleKeyword")` — クラス名ベース → **registry 内で解決 → 別 Base だと動かない**

### 対処方針

- cross-base の Relationship は**定義しない**。FK カラムのみ持つ
- JOIN が必要な場合は明示的クエリで書く
- 対象モデルを DeclarativeBase に移行した時点で Relationship を追加する

### 安全な組み合わせ

| 方向 | 手段 | 動作 |
|---|---|---|
| DeclarativeBase → DeclarativeBase | relationship() | OK（同一 registry） |
| SQLModel → SQLModel | Relationship() | OK（同一 registry） |
| SQLModel → DeclarativeBase | ForeignKey のみ | OK（metadata 共有） |
| DeclarativeBase → SQLModel | ForeignKey のみ | OK（metadata 共有） |
| **cross-base で relationship()** | — | **NG（registry 不一致）** |

### 本プランの cross-base 境界

```
ArticleKeyword (DeclarativeBase) ←FK→ NewsArticle (SQLModel)
```

- `ArticleKeyword.news_article` relationship → **削除**（cross-base）
- `NewsArticle.article_keywords` Relationship → **削除**（cross-base）
- FK カラム `article_keywords.news_article_id → news_articles.id` は維持

## 移行対象

| モデル | 移行先 | 理由 |
|---|---|---|
| Category | DeclarativeBase | CategorySlug, CategoryName の VO 化 |
| Keyword | DeclarativeBase | KeywordName の VO 化 + Category と同一 Base |
| ArticleKeyword | DeclarativeBase | Keyword と同一 Base にして relationship を維持 |
| 他の全モデル | SQLModel のまま | VO が不要、段階的に移行 |

### cross-base 境界の設計判断

ArticleKeyword も DeclarativeBase に移行し、cross-base 境界を `ArticleKeyword ↔ NewsArticle` に置く。

```
DeclarativeBase: Category ↔ Keyword ↔ ArticleKeyword  ✅ (relationship OK)
cross-base:      ArticleKeyword ✗ NewsArticle          (FK のみ、relationship NG)
SQLModel:        NewsArticle                            ✅
```

**理由:**
- ArticleKeyword は単純な中間テーブル（2カラム + relationship）で移行コストが極小
- Keyword ↔ ArticleKeyword の relationship が維持され、keywords.py の記事数カウント JOIN がそのまま動く
- cross-base 境界がどこにあっても news.py の selectinload チェーンは書き換えが必要なため、追加コストなし

## 実装ステップ

DB スキーマは変更なし。Alembic マイグレーション不要。

### Step 1: 基盤 — DeclarativeBase の定義 ✅

**新規**: `app/models/base.py`

- `Base(DeclarativeBase)` を定義
- `Base.metadata = SQLModel.metadata` で既存 metadata を共有（向きが重要）
- `type_annotation_map` に VO → TypeDecorator のマッピングを登録

### Step 2: VO 層 — RootModel への書き換え ✅

**変更**: `app/domain/category.py`, `app/domain/keyword.py`

- 手書きクラス (~90行) → `RootModel[str]` + `frozen=True` + `field_validator` (~15行)
- `__get_pydantic_core_schema__` 等のボイラープレートは全て削除

### Step 3: ORM 変換層 — TypeDecorator の作成 ✅

**新規**: `app/models/types.py`

- `CategorySlugType(TypeDecorator)` — `impl = String(50)`
- `CategoryNameType(TypeDecorator)` — `impl = String(50)`
- `KeywordNameType(TypeDecorator)` — `impl = String(100)`
- 全て `cache_ok = True` 必須（パフォーマンス）

#### 設計判断: process_bind_param で生の str も VO 経由でバリデーション

当初は生の `str` を `TypeError` で拒否する設計だったが、**str を受け取った場合も VO コンストラクタ経由でバリデーション**する方式に変更した。

```python
def process_bind_param(self, value, dialect):
    if value is None:
        return None
    if isinstance(value, CategorySlug):
        return value.root
    if isinstance(value, str):
        return CategorySlug(value).root  # VO 経由でバリデーション
    raise TypeError(f"Expected CategorySlug or str, got {type(value).__name__}")
```

**変更理由:**
- str → VO → str の変換でバリデーションは通るため、VO バイパスにはならない
- テストや既存サービスコード（`Keyword(name="...", ...)`）がそのまま動作
- `Keyword.name.in_(["str1", "str2"])` のような比較も自然に書ける

### Step 4: Category モデルの移行 ✅

**変更**: `app/models/category.py`

- `SQLModel, table=True` → `Base` 継承
- `Field(...)` → `mapped_column(...)`
- `slug: Mapped[CategorySlug]`, `name: Mapped[CategoryName]` — type_annotation_map で自動解決
- `__table_args__` の CHECK 制約は維持
- SQLAlchemy `relationship()` + `TYPE_CHECKING` import で forward reference 解決
- `model_rebuild()` + 末尾 import 削除

### Step 5: Keyword モデルの移行 ✅

**変更**: `app/models/keyword.py`

- 同様に `Base` 継承 + `Mapped` / `mapped_column`
- `name: Mapped[KeywordName]`
- `status: Mapped[str] = mapped_column(String(20), default=KeywordStatus.PROVISIONAL)` — DB ENUM を作らないよう String を明示
- `category`, `article_keywords` の relationship は同一 Base 内で維持
- `TYPE_CHECKING` import で `Category`, `ArticleKeyword` を解決
- `model_rebuild()` + 末尾 import 削除

### Step 5.5: ArticleKeyword モデルの移行 ✅

**変更**: `app/models/associations.py`

- `Base` 継承 + `mapped_column(ForeignKey(...), primary_key=True)`
- `keyword` relationship → 維持（同一 Base）
- `news_article` relationship → **削除**（cross-base: NewsArticle は SQLModel のまま）
- `TYPE_CHECKING` import で `Keyword` を解決
- `model_rebuild()` + 末尾 import 削除

**連動変更**: `app/models/news.py`

- `article_keywords` Relationship → **削除**（cross-base）
- `ArticleKeyword` の forward ref import 削除

### Step 6: スキーマ・サービス層の調整 ✅

#### 6-1. news.py — selectinload チェーンの書き換え

`NewsArticle.article_keywords` relationship が削除されたため、`_load_article_keywords()` ヘルパーで別クエリに分離。

```python
async def _load_article_keywords(
    session: AsyncSession, article_ids: list[int]
) -> dict[int, list[KeywordBrief]]:
    """ArticleKeyword 起点で keyword + category を selectinload。"""
    stmt = (
        select(ArticleKeyword)
        .options(selectinload(ArticleKeyword.keyword).selectinload(Keyword.category))
        .where(ArticleKeyword.news_article_id.in_(article_ids))
    )
    ...
```

- `_news_eager_options()` から `article_keywords` チェーンを除去
- `_build_news_response()` に `keywords_map` 引数を追加
- 全エンドポイント（`list_news`, `get_news`, `get_similar_news`）で `_load_article_keywords` を呼ぶ

#### 6-2. keywords.py — VO 手動 unwrap の削除

TypeDecorator が str を受入れるため、`.value` による手動 unwrap が不要に:

```python
# Before
name_value = body.name.value
existing = ... Keyword.name == name_value
keyword = Keyword(name=name_value, ...)

# After
existing = ... Keyword.name == body.name
keyword = Keyword(name=body.name, ...)
```

#### 6-3. スキーマの name フィールドを KeywordName に統一

TypeDecorator が `Keyword.name` を `KeywordName` VO として返すようになったため、
Pydantic スキーマの `name: str` フィールドが VO を拒否する問題が発生。

`RootModel[str]` は `str` のサブクラスではないため、Pydantic の str バリデータが拒否する。

**対処**: レスポンススキーマの `name` フィールドを `KeywordName` に変更

| スキーマ | 変更 |
|---|---|
| `KeywordResponse.name` | `str` → `KeywordName` |
| `KeywordBrief.name` | `str` → `KeywordName` |
| `KeywordInCategory.name` | `str` → `KeywordName` |

JSON 出力は同一（RootModel は root 値を plain string として出力）。
OpenAPI スキーマは `$ref` + `$defs` 経由になるが、解決後の型は `type: string` で互換。

#### 6-4. ai_analyzer.py — keyword dict の str 変換

AI プロンプトに渡す `keywords_by_category` dict は plain string が正しいため、
query 結果の VO を明示的に `str()` 変換:

```python
for slug, kw in rows:
    kw_dict.setdefault(str(slug), []).append(str(kw))
```

### Step 7: Alembic 互換性の確認（未実施）

- `alembic revision --autogenerate` で差分がゼロであることを確認
- `Base.metadata = SQLModel.metadata` により DeclarativeBase テーブルも同じ metadata に登録される

### Step 8: 検証

- `ruff check` + `ruff format` ✅
- `pytest` (216 tests passed) ✅
- `alembic revision --autogenerate -m "verify_no_diff"` → 空であること（未実施）

## リスクと対策

| リスク | 影響度 | 対策 | 結果 |
|---|---|---|---|
| cross-base Relationship 定義でランタイムエラー | 高 | ArticleKeyword ↔ NewsArticle 間は FK のみ | ✅ 解決 |
| news.py の selectinload チェーン破壊 | 高 | `_load_article_keywords()` で別クエリに分離 | ✅ 解決 |
| Alembic が DeclarativeBase テーブルを認識しない | 中 | metadata 共有で回避。Step 7 で検証 | 未検証 |
| RootModel の JSON シリアライズ形式変更 | 低 | RootModel は root をプリミティブとして出力。互換性あり | ✅ 問題なし |
| TypeDecorator の cache_ok 未設定 | 中 | 全 TypeDecorator に `cache_ok = True` を必須とする | ✅ 対応済 |
| VO がスキーマの str フィールドに渡せない | 中 | スキーマの name を KeywordName に変更 | ✅ 解決 |
| CHECK 制約と VO ルールの重複 | 低 | 移行完了後に見直し。下記「後続タスク」参照 | 未対応 |

## 確認済み事項

- **循環 import なし**: `app/domain/` → `app/models/` への依存ゼロ。`base.py` → `domain/` → 外部パッケージのみ
- **Pydantic メソッド依存なし**: Category/Keyword/ArticleKeyword に対する `.model_dump()`, `.model_validate()`, 直接 return は使用されていない。全てスキーマ経由で変換済み
- **metadata 共有の向き**: `Base.metadata = SQLModel.metadata`（クラス変数として定義時に確定。後から代入しない）
- **ArticleKeyword の移行安全性**: 単純な中間テーブル（2 FK カラム + relationship のみ）で VO 不要。TypeDecorator は使わず Base 継承 + mapped_column のみ
- **TYPE_CHECKING import**: forward reference は `if TYPE_CHECKING:` ブロックで解決。ruff の UP037/F821 を回避

## 後続タスク（このスコープ外）

- **CHECK 制約の見直し**: c14-c16 で追加した正規表現系 CHECK 制約は VO と責務が重複する。移行完了後に DB 層の制約を「NOT NULL + VARCHAR 長さ」に縮小し、パターン検証は VO 層に一本化するか検討する
