# DB リファクタリング実装プラン

> 作成日: 2026-03-22
> 方針: エンティティ単位で段階的に実施。FK 依存順序に沿って進行。
> 各 Phase 完了後に検証プロトコル（ruff + pytest + biome + tsc）を実行。

## Phase 概要

| Phase | エンティティ | 影響ファイル数 | 前提 | 難易度 |
|-------|-------------|--------------|------|--------|
| 1 | Categories | 22 | なし | 中 |
| 2 | Keywords + article_keywords | ~25 | Phase 1 | 中 |
| 3 | NewsSource | 24 | なし（独立） | 高（パイプライン変更を伴う） |
| 4 | NewsArticle | 未調査 | Phase 3 | 中 |
| 5 | ArticleAnalysis | 未調査 | Phase 4 | 中 |
| 6 | WatchlistEntry | 小 | Phase 4 | 低 |
| 7 | クリーンアップ | 小 | Phase 1-6 | 低 |

---

## Phase 1: Categories（keyword_categories → categories）

### Alembic マイグレーション
- `keyword_categories` → `categories` テーブルリネーム
- `keyword_category_translations` テーブル削除
- `investment_categories` テーブル削除
- `investment_category_translations` テーブル削除
- `analysis_investment_categories` テーブル削除
- ※ `keyword_category_links` は Phase 2 で Keywords と同時に廃止

### バックエンド変更（10ファイル）
- `models/keyword_category.py` → `models/category.py` リネーム。`KeywordCategory` → `Category`。Translation/Link クラスは Phase 2 まで維持
- `models/__init__.py` — エクスポート変更
- `schemas/keyword_category.py` → `schemas/category.py` リネーム。`KeywordCategoryBrief` → `CategoryBrief` 等
- `schemas/__init__.py` — エクスポート変更
- `routers/keyword_categories.py` → `routers/categories.py` リネーム。エンドポイント `/api/v1/keyword-categories` → `/api/v1/categories`
- `routers/news.py` — インポート変更
- `routers/keywords.py` — インポート変更
- `routers/me.py` — インポート変更
- `main.py` — ルーターインポート変更
- `services/ai_analyzer.py` — investment_categories 関連コード削除

### テスト変更（3ファイル）
- `test_keyword_categories.py` → `test_categories.py` リネーム + 修正
- `test_keywords.py` — インポート修正
- `conftest.py` — フィクスチャ修正

### フロントエンド変更（5ファイル + gen-types）
- `types/index.ts` — 型リネーム
- `lib/api-client.ts` — エンドポイントパス `/keyword-categories` → `/categories`
- `components/keywords/KeywordTag.tsx` — 型インポート変更
- `components/keywords/AddKeywordDialog.tsx` — 型インポート変更
- `components/layout/CategorySidebar.tsx` — 型インポート変更
- `components/layout/MobileSidebar.tsx` — 型インポート変更
- `types/generated.ts` — `npm run generate-types` で自動再生成

---

## Phase 2: Keywords + article_keywords

### 前提: Phase 1 完了

### Alembic マイグレーション
- `keywords.keyword` → `keywords.name` カラムリネーム
- `keywords.category_id` FK 追加（NOT NULL, REFERENCES categories(id) ON DELETE RESTRICT）
- データ移行: `keyword_category_links` の既存データを `keywords.category_id` に移行
- `keyword_category_links` テーブル削除
- `keywords.status` 追加（VARCHAR(20), DEFAULT 'official', CHECK制約）
- `keywords.is_ai_generated` 追加（Boolean, DEFAULT false）
- `keywords.approved_at` 追加（TIMESTAMPTZ, NULLABLE）
- `news_keywords` → `article_keywords` テーブルリネーム
- `user_keyword_subscriptions` テーブル削除（GAP-8）

### バックエンド変更
- `models/keyword.py` — `keyword` → `name`、`category_links` → `category_id` FK + `category` Relationship
- `models/category.py` — `KeywordCategoryLink` クラス削除
- `models/associations.py` — テーブル名 `news_keywords` → `article_keywords`
- `models/user_keyword.py` — 削除（user_keyword_subscriptions 廃止）
- `schemas/keyword.py` — 全スキーマで `keyword` → `name`、`category_id` 追加、`status` 追加
- `routers/keywords.py` — `kw.keyword` → `kw.name`、M:N JOIN → 1:N FK に変更
- `routers/categories.py` — JOIN ロジック変更
- `routers/news.py` — `link.keyword.keyword` → `link.keyword.name`、フィルタ変更
- `routers/me.py` — subscription 関連削除
- `services/ai_analyzer.py` — `Keyword.keyword` → `Keyword.name`、JOIN 変更

### テスト変更
- `conftest.py` — `Keyword(keyword=...)` → `Keyword(name=...)`
- `test_keywords.py` — アサーション + リクエストボディ修正
- `test_me.py` — 同上
- `test_news.py` — `keyword` → `name` アサーション修正
- `test_ai_analyzer.py` — 同上

### フロントエンド変更
- `lib/client-api.ts` — リクエストボディの `keyword` → `name`
- `components/keywords/KeywordRow.tsx` — `keyword.keyword` → `keyword.name`
- `components/news/NewsCard.tsx` — `kw.keyword` → `kw.name`
- `components/layout/Sidebar.tsx` — `kw.keyword` → `kw.name`
- `types/generated.ts` — gen-types で自動再生成

---

## Phase 3: NewsSource（最大・最難）

### 独立して実施可能だが、影響範囲が大きいため Phase 1, 2 完了後に実施推奨

### Alembic マイグレーション
- `endpoint_url` カラム追加（VARCHAR(2048), NOT NULL, UNIQUE）
- データ移行: `feed_url` / `api_endpoint` → `endpoint_url` に統合
- `feed_url`, `api_endpoint` カラム削除
- `name` VARCHAR(200) → VARCHAR(50) 縮小（既存データの検証が必要）
- `site_url` を NOT NULL に変更（既存 NULL データの対応が必要）
- 削除カラム: `etag`, `last_modified_header`, `fetch_interval_minutes`, `next_fetch_at`, `last_fetched_at`, `consecutive_errors`, `last_error_message`

### パイプラインアーキテクチャ変更（DB 変更と同時に必要）
- **Redis 実装**: etag / last_modified_header の保存・取得
- **スケジューラ変更**: `next_fetch_at` 廃止 → 新しいスケジューリング方式（環境変数/config ベース）
- **エラーカウント移行**: `consecutive_errors` / `last_error_message` → ワーカー/ログ
- **last_fetched_at 導出**: `fetch_logs` からの MAX クエリに変更

### バックエンド変更（7ファイル）
- `models/news_source.py` — 9カラム削除、endpoint_url 追加
- `schemas/news_source.py` — Create/Update/Response 全面書き換え、バリデータ変更
- `routers/news_sources.py` — CRUD ロジック変更、next_fetch_at 関連削除
- `services/news_fetcher.py` — etag/last_modified を Redis で管理、feed_url → endpoint_url
- `services/hacker_news.py` — last_fetched_at の取得方法変更（fetch_logs 導出）
- `services/alpha_vantage.py` — 同上
- `tasks/taskiq_worker.py` — スケジューリングロジック全面書き換え

### テスト変更（8ファイル）
- `conftest.py` — フィクスチャ修正
- `test_news_sources.py` — CRUD テスト全面修正
- `test_news_fetcher.py` — etag/last_modified テスト修正
- `test_taskiq_worker.py` — スケジューラテスト修正
- `test_hacker_news.py` — last_fetched_at テスト修正
- `test_alpha_vantage.py` — 同上
- `test_fetch_logs.py` — FK 確認
- `test_semantic_search.py` — フィクスチャ修正

### フロントエンド変更（4ファイル + gen-types）
- `components/sources/SourceFormDialog.tsx` — feedUrl/apiEndpoint → endpointUrl、fetchInterval 削除
- `components/sources/SourceTable.tsx` — エラー表示削除、URL表示変更、lastFetchedAt 削除
- `components/sources/SourceManager.tsx` — 影響確認
- `lib/client-api.ts` — 型変更に追従
- `types/generated.ts` — gen-types で自動再生成

---

## Phase 4: NewsArticle

### 前提: Phase 3 完了（news_source_id FK の変更）

### Alembic マイグレーション
- `source` カラム削除（GAP-10 レガシー）
- `url` → `original_url` リネーム
- `source_id` → `news_source_id` リネーム、NULLABLE → NOT NULL、SET NULL → RESTRICT
- `fetched_at` → `created_at` に統合（既存データ移行）
- `content_fetched_at` 削除
- `content_fetch_attempts` 削除
- `embedding` 削除（Phase 5 で article_analyses に追加）
- `article_group_id` 削除（NewsEvent 保留）
- `guid` 削除（Redis に移行）
- `description_original` TEXT → VARCHAR(2000)

### 影響範囲: Phase 実施時に調査

---

## Phase 5: ArticleAnalysis

### 前提: Phase 4 完了

### Alembic マイグレーション
- `analyses` → `article_analyses` テーブルリネーム
- UNIQUE制約変更: `(news_article_id, ai_model_id)` → `news_article_id` 単独
- `ai_model_id` FK 削除 → `ai_model` VARCHAR(100) 追加
- `sentiment` カラム削除
- `impact_score` → `impact_level` VARCHAR(20) に変更（データ移行: 数値→enum マッピング）
- `translated_title` VARCHAR(500) 追加
- `summary` TEXT 追加（analysis_translations から移行）
- `reasoning` TEXT → NOT NULL に変更
- `embedding` VECTOR(768) 追加（news_articles から移行）
- `embedding_model` VARCHAR(100) 追加
- `ai_models` テーブル削除
- `analysis_translations` テーブル削除

### 影響範囲: Phase 実施時に調査

---

## Phase 6: WatchlistEntry

### 前提: Phase 4 完了（news_articles テーブル変更完了）

### Alembic マイグレーション
- `watchlists` → `watchlist_entries` テーブルリネーム
- `id` serial PK → `(user_id, news_article_id)` 複合PK に変更
- `user_id` に FK 追加: `REFERENCES auth.user(id) ON DELETE CASCADE`

### 影響範囲: 小（モデル + スキーマ + ルーター + テスト）

---

## Phase 7: クリーンアップ

- Phase 1-6 で削除漏れがあったテーブルの一括削除
- 不要なインデックスの削除
- Alembic マイグレーション履歴の整理

---

## 検証プロトコル（各 Phase 完了時に実行）

```bash
# Backend
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Frontend
cd frontend && npx biome check src/ && npx tsc --noEmit
```

## 注意事項

- 各 Phase は独立したブランチで作業し、マージしてから次の Phase に進む
- Alembic マイグレーションは downgrade スクリプトも必ず記述する
- データ移行を伴うマイグレーション（Phase 2 の keyword_category_links → category_id、Phase 3 の feed_url/api_endpoint → endpoint_url 等）は特に慎重にテストする
- フロントエンドの型は `npm run generate-types` で自動再生成。手動編集禁止
- Phase 3 はパイプラインのアーキテクチャ変更を伴うため、最も時間がかかる見込み
- Phase 4, 5 の影響範囲は Phase 実施時にサブエージェントで調査する
