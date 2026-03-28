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

## 移行対象

| モデル | 移行先 | 理由 |
|---|---|---|
| Category | DeclarativeBase | CategorySlug, CategoryName の VO 化 |
| Keyword | DeclarativeBase | KeywordName の VO 化 + Category と同一 Base |
| 他の全モデル | SQLModel のまま | VO が不要、段階的に移行 |

## 実装ステップ

DB スキーマは変更なし。Alembic マイグレーション不要。

### Step 1: 基盤 — DeclarativeBase の定義

**新規**: `app/models/base.py`

- `Base(DeclarativeBase)` を定義
- `Base.metadata = SQLModel.metadata` で既存 metadata を共有（向きが重要）
- `type_annotation_map` に VO → TypeDecorator のマッピングを登録

### Step 2: VO 層 — RootModel への書き換え

**変更**: `app/domain/category.py`, `app/domain/keyword.py`

- 手書きクラス (~90行) → `RootModel[str]` + `frozen=True` + `field_validator` (~15行)
- `__get_pydantic_core_schema__` 等のボイラープレートは全て削除

### Step 3: ORM 変換層 — TypeDecorator の作成

**新規**: `app/models/types.py`

- `CategorySlugType(TypeDecorator)` — `impl = String(50)`
- `CategoryNameType(TypeDecorator)` — `impl = String(50)`
- `KeywordNameType(TypeDecorator)` — `impl = String(100)`
- 全て `cache_ok = True` 必須（パフォーマンス）

#### 設計判断: process_bind_param で生の str を拒否する

`process_bind_param` に生の `str` が来た場合、そのまま通さず `TypeError` にする。
str を素通しさせると VO のバリデーションをバイパスでき、VO を導入した意味がなくなるため。

```python
def process_bind_param(self, value, dialect):
    if value is None:
        return None
    if isinstance(value, CategorySlug):
        return value.root
    raise TypeError(f"Expected CategorySlug, got {type(value).__name__}")
```

### Step 4: Category モデルの移行

**変更**: `app/models/category.py`

- `SQLModel, table=True` → `Base` 継承
- `Field(...)` → `mapped_column(...)`
- `__tablename__ = "categories"` を明示指定（現状も明示済み。DeclarativeBase のデフォルト命名は SQLModel と異なる可能性があるため、省略厳禁）
- `slug: Mapped[CategorySlug]` — type_annotation_map で自動解決
- `__table_args__` の CHECK 制約は維持
- `keywords: Mapped[list["Keyword"]] = relationship(...)` — 同一 Base、問題なし

### Step 5: Keyword モデルの移行

**変更**: `app/models/keyword.py`

- 同様に `Base` 継承 + `Mapped` / `mapped_column`
- `__tablename__ = "keywords"` を明示指定（同上の理由）
- `name: Mapped[KeywordName]`
- `category: Mapped["Category"] = relationship(...)` — 同一 Base、問題なし
- `article_keywords` — **cross-base のため Relationship は削除**、FK カラムのみ維持

### Step 6: スキーマ・サービス層の調整

TypeDecorator の自動変換により、多くの箇所は変更不要。書き換えが必要な箇所を以下に整理する。

#### 書き換え不要（TypeDecorator が自動処理）

- DB 読み取り後の型: `category.slug` が自動的に `CategorySlug` になる
- ルーター層の応答組み立て: `CategoryBrief(slug=category.slug, ...)` はそのまま動く
- WHERE 句: `Keyword.name == body.name` — TypeDecorator が bind param を変換

#### 書き換えが必要な箇所

**6-1. VO の手動 unwrap を削除** (`app/routers/keywords.py`)

```python
# Before
name_value = body.name.value                              # L70: 手動 unwrap
existing = ... Keyword.name == name_value                 # L72: str で比較
keyword = Keyword(name=name_value, category_id=...)       # L87: str を渡す

# After
existing = ... Keyword.name == body.name                  # VO 直接比較
keyword = Keyword(name=body.name, category_id=...)        # VO 直接渡し
```

**6-2. テスト — str リテラルを VO に変更**

| ファイル | 行 | 変更 |
|---|---|---|
| `tests/conftest.py` | L167 | `Category(slug=slug, name=name)` → `Category(slug=CategorySlug(slug), name=CategoryName(name))` |
| `tests/conftest.py` | L182 | `Keyword(name="...", ...)` → `Keyword(name=KeywordName("..."), ...)` |
| `tests/test_routers/test_categories.py` | L81, L111 | 同上 |
| `tests/test_ai_analyzer.py` | L432-434 | 同上 |

**6-3. テスト — `.value` アクセスを `.root` に変更**

| ファイル | 行 | 変更 |
|---|---|---|
| `tests/test_domain/test_category_values.py` | L189-190, L248-249 | `.slug.value` → `.slug.root` |

**6-4. export の更新**

- `app/models/__init__.py`: 必要に応じて import パスを調整

### Step 7: Alembic 互換性の確認

- `alembic revision --autogenerate` で差分がゼロであることを確認
- `Base.metadata = SQLModel.metadata` により DeclarativeBase テーブルも同じ metadata に登録される

### Step 8: 検証

- `ruff check` + `ruff format`
- `pytest`
- `alembic revision --autogenerate -m "verify_no_diff"` → 空であること

## リスクと対策

| リスク | 影響度 | 対策 |
|---|---|---|
| cross-base Relationship 定義でランタイムエラー | 高 | 定義しない。FK のみ。上記制約を遵守 |
| Alembic が DeclarativeBase テーブルを認識しない | 中 | metadata 共有で回避。Step 7 で検証 |
| RootModel の JSON シリアライズ形式変更 | 低 | RootModel は root をプリミティブとして出力。互換性あり |
| TypeDecorator の cache_ok 未設定 | 中 | 全 TypeDecorator に `cache_ok = True` を必須とする |
| CHECK 制約と VO ルールの重複 | 低 | 移行完了後に見直し。下記「後続タスク」参照 |

## 確認済み事項

- **循環 import なし**: `app/domain/` → `app/models/` への依存ゼロ。`base.py` → `domain/` → 外部パッケージのみ
- **Pydantic メソッド依存なし**: Category/Keyword に対する `.model_dump()`, `.model_validate()`, 直接 return は使用されていない。全てスキーマ経由で変換済み
- **metadata 共有の向き**: `Base.metadata = SQLModel.metadata`（クラス変数として定義時に確定。後から代入しない）

## 後続タスク（このスコープ外）

- **CHECK 制約の見直し**: c14-c16 で追加した正規表現系 CHECK 制約は VO と責務が重複する。移行完了後に DB 層の制約を「NOT NULL + VARCHAR 長さ」に縮小し、パターン検証は VO 層に一本化するか検討する
