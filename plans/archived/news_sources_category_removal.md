# news_sources.category_id 削除 & 記事レベルキーワード選択

## Context

`news_sources` テーブルの `category_id` は「ソース = 1カテゴリ」という不自然な制約を持つ。
TechCrunch は AI・バイオテック・金融など多ジャンルの記事を配信するため、ソース単位でカテゴリを固定するのは不適切。
現状 `category_id` は AI 分析時のキーワード候補絞り込みに使われているが、この設計ではバイオテック記事に AI カテゴリのキーワードしか渡されない問題がある。

**方針**: `category_id` を `news_sources` から削除し、AI 分析時に全カテゴリのキーワードを渡す。AIが記事内容に基づいて適切なキーワードを選択する。

**決定事項**:
- `analyses.keyword_category_id` は追加しない（YAGNI: キーワード経由の間接パスで CategorySidebar のフィルタリングは正常動作する）
- 既存分析済み記事の再分析は行わない（新記事から自然解消）

**副次効果**: `news_sources.category_id` の `ON DELETE CASCADE` リスクが解消される（`keyword_categories` 削除時に `news_sources` が連鎖削除されなくなる）

---

## 影響範囲の確認

### 変更不要（影響なし）

| コンポーネント | 理由 |
|---------------|------|
| `frontend/src/components/layout/CategorySidebar.tsx` | `keyword_category_links` → `news_keywords` 経由で計算。`news_sources.category_id` を参照していない |
| `backend/app/routers/news.py` の `kwCategoryId` フィルタ | 同上。`keyword_category_links` → `news_keywords` 経由 |
| `backend/app/routers/keyword_categories.py` | `articleCount` は `news_keywords` 経由で計算 |
| `frontend/src/components/news/NewsCard.tsx` | カテゴリ表示はキーワード・分析結果経由 |
| `frontend/src/components/news/NewsFilters.tsx` | 同上 |
| `frontend/src/lib/client-api.ts` | ソース API 呼び出しは `SourceFormDialog.tsx` 内で直接 fetch |

---

## Step 1: Backend モデル変更

### `backend/app/models/news_source.py`
- `category_id` フィールド（L20-26）を削除
- `category` リレーション（L58）を削除
- `KeywordCategory` の import を削除

### `backend/app/models/keyword_category.py`
- `KeywordCategory.sources` リレーション（L16）を削除
- `NewsSource` の forward reference import を削除

## Step 2: Pydantic スキーマ変更（SSoT）

### `backend/app/schemas/news_source.py`
- `NewsSourceCreate`: `category_id` フィールド削除
- `NewsSourceUpdate`: `category_id` フィールド削除
- `NewsSourceResponse`: `category_id` と `category_name` フィールド削除

## Step 3: Alembic マイグレーション — `category_id` カラム削除

**ファイル**: `backend/alembic/versions/` に新規マイグレーション作成

- `news_sources.category_id` カラムを DROP（FK制約も削除）
- downgrade で元に戻せるよう、`category_id` を再追加するロジックも記述
- 既存データに影響なし（カラム削除のみ）

## Step 4: AI 分析ロジック変更

### `backend/app/services/ai_analyzer.py`

**L60-84 — `BaseAnalyzer` 抽象メソッドのシグネチャ変更**:
- `keyword_candidates: list[str] | None` → `keywords_by_category: dict[str, list[str]] | None`

**L142-168 — キーワード候補取得ロジック全体を置換**:

Before: `source.category_id` → そのカテゴリのキーワードのみ取得（`if article.source_id is not None:` ガード付き）
After: `source_id` ガードを削除し、全カテゴリの全キーワードをカテゴリ別に整理して取得

```python
# 変更後のキーワード候補取得ロジック（source_id ガードなし）
keywords_by_category: dict[str, list[str]] | None = None
stmt = (
    select(KeywordCategory.slug, Keyword.keyword)
    .join(KeywordCategoryLink, KeywordCategoryLink.category_id == KeywordCategory.id)
    .join(Keyword, Keyword.id == KeywordCategoryLink.keyword_id)
)
rows = (await session.execute(stmt)).all()
if rows:
    kw_dict: dict[str, list[str]] = {}
    for slug, kw in rows:
        kw_dict.setdefault(slug, []).append(kw)
    keywords_by_category = kw_dict
```

- `analyze()` 呼び出し: `keyword_candidates=keyword_candidates` → `keywords_by_category=keywords_by_category`
- `NewsSource` の import を削除（不要になる）

### `backend/app/services/gemini_analyzer.py`

**`analyze()` メソッド**:
- `keyword_candidates: list[str] | None` → `keywords_by_category: dict[str, list[str]] | None`
- プロンプトにキーワード候補をカテゴリ別に渡す:

```
Additionally, select up to 3 keywords from the following candidates
that best describe this article's topic:

Keyword candidates by category:
- ai_ml: ["quantum", "machine-learning", "LLM", ...]
- biotech: ["CRISPR", "gene-therapy", ...]
- fintech: ["blockchain", "DeFi", ...]
```

**`_parse_response()` メソッド**:
- `keyword_candidates: list[str] | None` → `keywords_by_category: dict[str, list[str]] | None`
- バリデーション: 全カテゴリの全キーワードをフラット化して候補セットを作成し、AIが返したキーワードを照合

```python
if keywords_by_category:
    all_candidates = set()
    for kws in keywords_by_category.values():
        all_candidates.update(kws)
    # AI が返したキーワードを all_candidates と照合
```

## Step 5: ルーター変更

### `backend/app/routers/news_sources.py`
- `_get_category_name()` ヘルパー関数を削除
- `_to_response()` から `category_id`, `category_name` を削除
- `_source_eager_options()` から category の selectinload を削除
- `create_source()`: category_id のバリデーションを削除
- `update_source()`: category_id の更新ロジックを削除
- `KeywordCategory` の import を削除

## Step 6: フロントエンド変更

### `frontend/src/components/sources/SourceFormDialog.tsx`
- `categoryId` state と Category セレクト UI を削除（L55, L65, L73, L80, L87, L167-181）
- `categories` prop を削除
- submit body から `categoryId` を削除

### `frontend/src/components/sources/SourceTable.tsx`
- Category カラム（L102, L125）を削除
- `categories` prop を削除

### `frontend/src/components/sources/SourceManager.tsx`
- `categories` prop を `SourceManagerProps` から削除
- `SourceTable` と `SourceFormDialog` への `categories` 受け渡しを削除

### `frontend/src/app/(protected)/settings/page.tsx`
- `getKeywordCategories()` 呼び出し（L40-43）を削除
- `SourceManager` への `categories` prop 渡しを削除

### フロントエンド型再生成
- `npm run generate-types` で `generated.ts` を更新

## Step 7: シードマイグレーション修正

### `backend/alembic/versions/a3_seed_news_sources.py`
- INSERT 文から `category_id` を削除

## Step 8: テスト更新

### `backend/tests/conftest.py`
- テストフィクスチャの `NewsSource` 作成箇所から `category_id` を削除

### `backend/tests/test_routers/test_news_sources.py`
- テストデータから `category_id` を削除
- category 関連のアサーションを削除

### `backend/tests/test_ai_analyzer.py`
- キーワード候補の渡し方を `keywords_by_category` 形式に変更

## Step 9: ドキュメント更新

### `docs/02_DATABASE_DESIGN.md`
- `news_sources` テーブル定義から `category_id` 行を削除
- 関連するインデックス・制約の記述を更新

---

## 変更ファイル一覧

| ファイル | 変更内容 |
|---------|---------|
| `backend/alembic/versions/新規.py` | category_id DROP |
| `backend/app/models/news_source.py` | category_id, category relationship 削除 |
| `backend/app/models/keyword_category.py` | sources relationship 削除 |
| `backend/app/schemas/news_source.py` | category_id, category_name 削除 |
| `backend/app/services/ai_analyzer.py` | 全キーワード取得、source_id ガード削除、BaseAnalyzer シグネチャ変更 |
| `backend/app/services/gemini_analyzer.py` | プロンプト・パース変更 |
| `backend/app/routers/news_sources.py` | category 関連ロジック削除 |
| `frontend/src/components/sources/SourceFormDialog.tsx` | category UI 削除 |
| `frontend/src/components/sources/SourceTable.tsx` | category カラム削除 |
| `frontend/src/components/sources/SourceManager.tsx` | categories prop 削除 |
| `frontend/src/app/(protected)/settings/page.tsx` | getKeywordCategories 呼び出し削除 |
| `backend/alembic/versions/a3_seed_news_sources.py` | category_id 削除 |
| `backend/tests/conftest.py` | フィクスチャ更新 |
| `backend/tests/test_routers/test_news_sources.py` | テスト更新 |
| `backend/tests/test_ai_analyzer.py` | テスト更新 |
| `docs/02_DATABASE_DESIGN.md` | テーブル定義更新 |

## 検証手順

```bash
# 1. Backend lint + test
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# 2. マイグレーション実行
cd backend && alembic upgrade head

# 3. Frontend lint + type check
cd frontend && npx eslint src/ && npx tsc --noEmit

# 4. 型再生成
cd frontend && npm run generate-types

# 5. 動作確認
# - ソース管理画面でカテゴリ欄が消えていること
# - ソース作成/編集でカテゴリ選択UIがないこと
# - CategorySidebar のフィルタリングが正常に動作すること
```
