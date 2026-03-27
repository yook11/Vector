# Phase 7: クリーンアップ — レガシーコード・テーブル一括除去

> 作成日: 2026-03-26
> ブランチ: feature/better-auth (既存)
> スペック: `specs/db-redesign.md/plan.md` Phase 7
> 前提: **Phase 1-6 すべて完了後**に実施
> **スコープ: 開発環境限定。**

## 概要

Phase 1-6 で "Step 5 で削除" とマークされていたレガシーコード・テーブル・カラムを一括除去する。
3 ステップに分割し、各ステップ完了後に検証プロトコルを実行する。

### 削除対象サマリー

| 種類 | 対象 |
|------|------|
| テーブル | `article_groups`, `ai_models` |
| カラム (news_articles) | `title_original`, `url`, `source`, `fetched_at`, `content`, `content_fetched_at`, `content_fetch_attempts`, `source_id`, `guid`, `article_group_id`, `embedding` |
| インデックス | `idx_news_fetched`, `idx_article_groups_canonical` |
| FK | `fk_news_articles_article_group_id`, `fk_article_groups_canonical_id`, `news_articles.source_id → news_sources.id` |

---

## Step A: ArticleGroup 系の除去

### 目的

`article_groups` テーブルと、それに依存する dedup UI・group endpoint を一括除去する。
重複検出はログベースに移行済み（`services/dedup.py` が `ArticleGroup` を使わない形で実装済み）。

### ロールバック戦略

Step A-1 〜 A-3 は一連の作業として連続実施し、中間状態でのデプロイは行わない。
Alembic downgrade で article_groups テーブル + article_group_id カラムを復元可能。

---

### Step A-1: Backend コード変更

#### models/article_group.py — 削除

ファイルごと削除。

#### models/news.py — レガシー部分除去

| 変更 | 詳細 |
|------|------|
| カラム削除 | `article_group_id` |
| Relationship 削除 | `article_group` |
| Import 削除 | `from app.models.article_group import ArticleGroup` |

#### models/__init__.py — export 修正

- `from app.models.article_group import ArticleGroup` 行を削除
- `__all__` から `"ArticleGroup"` を削除

#### schemas/news.py — レガシーフィールド除去

| 削除 | 詳細 |
|------|------|
| `duplicate_count: int = 0` | Legacy フィールド |
| `article_group_id: int \| None = None` | Legacy フィールド |

#### routers/news.py — ArticleGroup 関連ロジック除去

| 変更 | 詳細 |
|------|------|
| Import 削除 | `from app.models.article_group import ArticleGroup` |
| `_build_news_response()` | `article_group` / `duplicate_count` / `article_group_id` 参照削除 |
| `_news_eager_options()` | `selectinload(NewsArticle.article_group)` 削除 |
| `list_news()` | `deduplicated` パラメータ + ArticleGroup dedup filter 削除 |
| `get_group_articles()` | エンドポイント `/news/groups/{group_id}` を丸ごと削除 |

#### routers/categories.py — dedup ロジック除去

| 変更 | 詳細 |
|------|------|
| Import 削除 | `from app.models.article_group import ArticleGroup` |
| `list_categories()` | `canonical_ids` サブクエリ + `visible_article_ids` の dedup 条件削除 → 全記事を対象にカウント |

---

### Step A-2: Frontend コード変更

#### components/news/DuplicateBadge.tsx — 削除

ファイルごと削除。

#### components/news/NewsCard.tsx

- `DuplicateBadge` import 削除
- `article.duplicateCount > 0 && article.articleGroupId != null` ブロック削除

#### lib/client-api.ts

- `clientGetGroupArticles()` 関数を削除

#### lib/api-client.ts

- `getGroupArticles()` 関数を削除

---

### Step A-3: テスト修正

影響のあるテストファイルを確認し、`article_group_id` / `ArticleGroup` 参照を除去。

| ファイル | 変更内容 |
|---------|---------|
| `test_dedup.py` | `article_group_id` 参照があれば除去 |
| `test_news_fetcher.py` | フィクスチャの `article_group_id` 除去 |
| `test_hacker_news.py` | 同上 |
| `test_alpha_vantage.py` | 同上 |
| `conftest.py` | ArticleGroup フィクスチャがあれば削除 |

---

### Step A-4: Alembic マイグレーション

マイグレーション名: `c8_phase7a_drop_article_groups`

#### upgrade

```sql
-- 1. FK 削除（use_alter のため明示的に DROP）
ALTER TABLE news_articles DROP CONSTRAINT IF EXISTS fk_news_articles_article_group_id;
ALTER TABLE article_groups DROP CONSTRAINT IF EXISTS fk_article_groups_canonical_id;

-- 2. インデックス削除
DROP INDEX IF EXISTS idx_article_groups_canonical;

-- 3. カラム削除
ALTER TABLE news_articles DROP COLUMN IF EXISTS article_group_id;

-- 4. テーブル削除
DROP TABLE IF EXISTS article_groups;
```

#### downgrade

```sql
-- 1. テーブル復元
CREATE TABLE article_groups (
    id SERIAL PRIMARY KEY,
    canonical_id INTEGER,
    article_count INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_article_groups_canonical ON article_groups (canonical_id);

-- 2. カラム復元
ALTER TABLE news_articles ADD COLUMN article_group_id INTEGER;
CREATE INDEX ON news_articles (article_group_id) WHERE article_group_id IS NOT NULL;

-- 3. FK 復元
ALTER TABLE news_articles ADD CONSTRAINT fk_news_articles_article_group_id
    FOREIGN KEY (article_group_id) REFERENCES article_groups(id) ON DELETE SET NULL;
ALTER TABLE article_groups ADD CONSTRAINT fk_article_groups_canonical_id
    FOREIGN KEY (canonical_id) REFERENCES news_articles(id) ON DELETE SET NULL;
```

### Step A-5: gen-types + 検証プロトコル

```bash
# Backend
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Frontend
cd frontend && npm run generate-types && npx biome check src/ && npx tsc --noEmit
```

---

## Step B: AIModel テーブル除去

### 目的

Phase 4 で `article_analyses.ai_model` を VARCHAR カラムに移行済み。
不要になった `ai_models` テーブルとモデルを削除。

---

### Step B-1: Backend コード変更

#### models/ai_model.py — 削除

ファイルごと削除。

#### models/__init__.py — export 修正

- `from app.models.ai_model import AIModel` 行を削除
- `__all__` から `"AIModel"` を削除

---

### Step B-2: Alembic マイグレーション

マイグレーション名: `c9_phase7b_drop_ai_models`

#### upgrade

```sql
DROP TABLE IF EXISTS ai_models;
```

#### downgrade

```sql
CREATE TABLE ai_models (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(20) NOT NULL,
    name VARCHAR(50) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    CONSTRAINT uq_ai_model_provider_name UNIQUE (provider, name)
);
```

### Step B-3: 検証プロトコル

```bash
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q
```

---

## Step C: news_articles レガシーカラム一括除去

### 目的

Phase 4 で新カラムに移行済みの旧カラム 9 個 + インデックスを削除。

### 対象カラム

| レガシーカラム | 移行先 | 備考 |
|--------------|--------|------|
| `title_original` | `original_title` | リネーム済み |
| `url` | `original_url` | リネーム済み |
| `source` | `news_source.name` (FK 経由) | 非正規化カラム廃止 |
| `fetched_at` | `created_at` | 統合済み |
| `content` | `original_content` | リネーム済み |
| `content_fetched_at` | — | 廃止（不要） |
| `content_fetch_attempts` | — | 廃止（不要） |
| `source_id` | `news_source_id` | FK 名変更 + NOT NULL 化済み |
| `guid` | Redis | DB 外に移行済み |
| `embedding` | `article_analyses.embedding` | テーブル移動済み |

### 削除インデックス

| インデックス | 対象カラム |
|-------------|-----------|
| `idx_news_fetched` | `fetched_at`（削除カラム） |

---

### Step C-1: Backend コード変更

#### models/news.py

- レガシーカラム 9 個（`title_original` 〜 `embedding`）の Field 定義を削除
- `idx_news_fetched` を `__table_args__` から削除
- `pgvector` import が不要になれば削除
- `source_id` 用の FK（`ondelete="SET NULL"`）削除

---

### Step C-2: テスト修正

| ファイル | 変更内容 |
|---------|---------|
| `test_news_fetcher.py` | レガシーカラムでの記事生成を新カラムに修正 |
| `test_content_extractor.py` | `content_fetched_at`, `content_fetch_attempts` 参照除去 |
| `test_fetch_logs.py` | `guid` 参照除去 |
| `test_hacker_news.py` | レガシーカラム参照を新カラムに修正 |
| `test_alpha_vantage.py` | 同上 |
| `test_embedding.py` | `NewsArticle.embedding` 参照を `ArticleAnalysis.embedding` に修正 |
| `test_semantic_search.py` | 同上 |
| `conftest.py` | フィクスチャのレガシーカラム除去 |

---

### Step C-3: Alembic マイグレーション

マイグレーション名: `c10_phase7c_drop_legacy_columns`

#### upgrade

```sql
-- 1. FK 削除（source_id の旧 FK）
ALTER TABLE news_articles DROP CONSTRAINT IF EXISTS news_articles_source_id_fkey;

-- 2. インデックス削除
DROP INDEX IF EXISTS idx_news_fetched;

-- 3. カラム一括削除
ALTER TABLE news_articles
    DROP COLUMN IF EXISTS title_original,
    DROP COLUMN IF EXISTS url,
    DROP COLUMN IF EXISTS source,
    DROP COLUMN IF EXISTS fetched_at,
    DROP COLUMN IF EXISTS content,
    DROP COLUMN IF EXISTS content_fetched_at,
    DROP COLUMN IF EXISTS content_fetch_attempts,
    DROP COLUMN IF EXISTS source_id,
    DROP COLUMN IF EXISTS guid,
    DROP COLUMN IF EXISTS embedding;
```

#### downgrade

```sql
-- カラム復元（nullable で追加、データ復旧は不可）
ALTER TABLE news_articles
    ADD COLUMN title_original VARCHAR(500),
    ADD COLUMN url VARCHAR(2048),
    ADD COLUMN source VARCHAR(100),
    ADD COLUMN fetched_at TIMESTAMPTZ DEFAULT now(),
    ADD COLUMN content TEXT,
    ADD COLUMN content_fetched_at TIMESTAMPTZ,
    ADD COLUMN content_fetch_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN source_id INTEGER,
    ADD COLUMN guid VARCHAR(2048),
    ADD COLUMN embedding vector(768);

-- 旧カラムに既存データからコピー
UPDATE news_articles SET
    title_original = original_title,
    url = original_url,
    source = '',
    fetched_at = created_at,
    content = original_content,
    source_id = news_source_id;

-- NOT NULL 制約復元
ALTER TABLE news_articles
    ALTER COLUMN title_original SET NOT NULL,
    ALTER COLUMN url SET NOT NULL,
    ALTER COLUMN source SET NOT NULL,
    ALTER COLUMN fetched_at SET NOT NULL;

-- インデックス・FK 復元
CREATE INDEX idx_news_fetched ON news_articles USING btree (fetched_at);
ALTER TABLE news_articles ADD CONSTRAINT news_articles_source_id_fkey
    FOREIGN KEY (source_id) REFERENCES news_sources(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX ON news_articles (guid) WHERE guid IS NOT NULL;
```

### Step C-4: gen-types + 検証プロトコル

```bash
# Backend
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Frontend
cd frontend && npm run generate-types && npx biome check src/ && npx tsc --noEmit
```

---

## 実施順序

```
Step A (ArticleGroup) → Step B (AIModel) → Step C (レガシーカラム)
     ↓ 検証                 ↓ 検証               ↓ 検証 + gen-types
```

各ステップ完了後にコミット。Step B は Step A と独立だが、Alembic chain の都合上 A → B → C の順で実施。
