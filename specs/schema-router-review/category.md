# Category — スキーマ / ルーターレビュー

## 対象ファイル

| レイヤー | ファイル |
|---|---|
| Model | `backend/app/models/category.py` |
| Schema | `backend/app/schemas/category.py` |
| Router | `backend/app/routers/categories.py` |

## スキーマ一覧

| クラス | 用途 | フィールド |
|---|---|---|
| `CategoryBrief` | 他レスポンスへの埋め込み用 | `slug: CategorySlug`, `name: CategoryName` |
| `KeywordInCategory` | カテゴリ詳細内のキーワード | `id: int`, `name: KeywordName`, `article_count: int` |
| `CategoryDetailResponse` | カテゴリ詳細レスポンス | `id: int`, `slug: CategorySlug`, `name: CategoryName`, `article_count: int`, `keywords: list[KeywordInCategory]` |
| `CategoryDetailListResponse` | リストエンドポイントのラッパー | `items: list[CategoryDetailResponse]` |

**Create / Update スキーマは存在しない**（カテゴリはシードデータで管理、API 経由の CRUD なし）。

## エンドポイント一覧

| メソッド | パス | 関数 | レスポンス |
|---|---|---|---|
| GET | `/api/v1/categories` | `list_categories()` | `CategoryDetailListResponse` |

## 確認項目と結果

### 1. VO 型の使用状況

| スキーマフィールド | 型 | モデルの型 | 状態 |
|---|---|---|---|
| `CategoryBrief.slug` | `CategorySlug` | `Mapped[CategorySlug]` | OK |
| `CategoryBrief.name` | `CategoryName` | `Mapped[CategoryName]` | OK |
| `KeywordInCategory.name` | `KeywordName` | `Mapped[KeywordName]` | OK |
| `CategoryDetailResponse.slug` | `CategorySlug` | `Mapped[CategorySlug]` | OK |
| `CategoryDetailResponse.name` | `CategoryName` | `Mapped[CategoryName]` | OK |

全フィールドで VO 型がスキーマ・モデル間で一致している。

### 2. ルーターでの str() 変換

str() 変換は **なし**。TypeDecorator の `process_result_value` で DB → VO 変換されたものがそのままスキーマに渡されている。

```python
# routers/categories.py L78-84 — VO がそのまま渡される
CategoryDetailResponse(
    id=row.id,
    slug=row.slug,      # CategorySlug (from TypeDecorator)
    name=row.name,      # CategoryName (from TypeDecorator)
    ...
)
```

### 3. camelCase 変換

全スキーマクラスに `alias_generator=to_camel`, `populate_by_name=True` が設定済み。

| フィールド | JSON キー | 状態 |
|---|---|---|
| `article_count` | `articleCount` | OK |
| `slug` | `slug` | OK（変換なし） |

### 4. model_config の重複

4 クラスすべてに同一の `model_config` が個別定義されている。共通ベースクラスへの抽出は可能だが、現時点では各クラスが明示的で読みやすいため、問題なしとする。

### 5. type: ignore コメント

ルーター L79-83 に `# type: ignore[arg-type]` が 3 箇所。`select()` で個別カラムを取得した際の Row 型と Pydantic コンストラクタの型の不一致による。実行時の問題はないが、型チェッカーが Row の型を正確に推論できないための抑制。

## 検証結果

### lint / format

```
ruff check  — All checks passed
ruff format — 2 files already formatted
```

### テスト

```
tests/test_routers/test_categories.py       — 8 passed
tests/test_domain/test_category_values.py   — 43 passed
合計: 51 passed
```

camelCase レスポンス (`articleCount`) もテストで検証済み（`test_article_count`）。

### DeprecationWarning: session.execute() vs session.exec()

ルーター内の 3 箇所で SQLModel の DeprecationWarning が発生:

```
L28: cat_result = await session.execute(cat_stmt)
L45: kw_result = await session.execute(kw_stmt)
L59: cat_count_result = await session.execute(cat_count_stmt)
```

本プロジェクトは DeclarativeBase + カラムレベル select (`select(Category.id, Category.slug, ...)`) を使用しており、Row オブジェクトを返す `session.execute()` が正しい選択。SQLModel の `session.exec()` はモデルレベルクエリ向けの糖衣構文であり、`func.count()` / `func.distinct()` 等の raw SQL expression 構文との相性は未検証。

**対応判断**: 全ルーター共通の課題のため、個別のスキーマ/ルーターレビューとは別スコープで検討する。

## 結論

**修正不要**。Category のスキーマ・ルーターは VO 型が正しく使用されており、str() 変換も存在しない。models 層で適用した VO がスキーマ層まで一貫して伝播している理想的な状態。
