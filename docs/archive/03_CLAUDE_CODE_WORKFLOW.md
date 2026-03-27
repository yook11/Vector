# Claude Code 開発ワークフロー & タスク分解 (v4)

## 開発の進め方

Claude Code のメインエージェントがオーケストレーターとして、
サブエージェントにタスクを委譲しながら並行開発する。

### 原則
1. **API契約ファースト**: DB設計 → APIスキーマの順で直列に定義 → フロント・バックが独立開発可能
2. **SSoTは Pydantic schemas**: `backend/app/schemas/` が型の源泉 → `npm run generate-types` で `frontend/src/types/generated.ts` を自動生成
3. **サブエージェントは独立**: 各サブエージェントは自分のCLAUDE.mdとPydanticスキーマだけで作業完結
4. **統合はメインが担当**: サブエージェントの成果物をメインが統合・テスト
5. **段階的統合テスト**: 各ステップ完了時に小規模な結合確認を挟む

## Phase 1 フロー図（完了）

```
Step 1: 初期化                          ✅
  ↓
Step 2: Docker環境構築                   ✅
  ↓
Step 3: DB設計 & マイグレーション         ✅ ← 直列（先にDBを確定）
  ↓
Step 4: APIスキーマ & 型生成             ✅ ← DBモデルから導出
  ↓
  ├─── 分岐A: バックエンド ─────────────┐
  │    Step 5: API実装              ✅  │
  │      ↓ [統合テスト①]                │
  │    Step 6: Fetcher              ✅  │
  │      ↓ [統合テスト②]                │    分岐B: フロントエンド
  │    Step 7: AI Service           ✅  │    Step 9: Frontend実装 ✅
  │      ↓ [統合テスト③]                │
  │    Step 8: タスクキュー          ✅  │
  │                                     │
  └──────────────┬──────────────────────┘
                 ↓
Step 10: 統合 & E2E                      ✅
```

## Phase 2 フロー図（完了）

```
Step A: 認証 (Better Auth BFF)             ✅
  ↓
Step B: ユーザー機能                      ✅
  (subscriptions, watchlist)
  ↓
Step C: 記事全文取得                      ✅
  (trafilatura + content_extractor)
  ↓
Step D: タスクキュー                      ✅
  (taskiq + Redis, APScheduler 撤去)
  ↓
Step E: pgvector セマンティック検索        ✅
  (embedding + similar articles)
```

## Phase 1 タスク分解（完了）

### ステップ 1: プロジェクト初期化 [メインエージェント] ✅
```
目的: 開発環境のセットアップ
成果物:
  - リポジトリ初期化 (git init)
  - ディレクトリ構成の生成
  - 全CLAUDE.md の配置
  - .env.example の作成
  - docker-compose.yml の作成
  - .gitignore の作成
```

### ステップ 2: Docker環境構築 [サブエージェント: infra] ✅
```
対象: docker-compose.yml, frontend/Dockerfile, backend/Dockerfile
CLAUDE.md: /CLAUDE.md を参照
成果物:
  - docker-compose.yml (frontend, backend, db)
  - frontend/Dockerfile (Node.js 20, Next.js dev server)
  - backend/Dockerfile (Python 3.12, FastAPI uvicorn)
  - `docker compose up` でフロント・バック・DBが起動する状態
検証:
  - localhost:3000 → Next.js デフォルトページ
  - localhost:8000/docs → FastAPI Swagger UI
  - PostgreSQL に接続可能
```

### ステップ 3: DB設計 & マイグレーション [サブエージェント: db] ✅
```
対象: backend/app/models/, backend/alembic/
CLAUDE.md: backend/CLAUDE.md を参照
ドキュメント: docs/02_DATABASE_DESIGN.md を参照

成果物:
  - SQLModel テーブル定義 (4テーブル)
    - models/keyword.py    → Keyword
    - models/news.py       → NewsArticle
    - models/analysis.py   → AnalysisResult
    - models/associations.py → NewsKeyword
  - Alembic 初期マイグレーション
  - db.py (AsyncSession 設定)
検証:
  - `alembic upgrade head` でテーブル作成
  - `alembic downgrade -1` でロールバック
  - psql でテーブル・リレーション確認
```

### ステップ 4: APIスキーマ定義 [サブエージェント: schema] ✅
```
対象: backend/app/schemas/
CLAUDE.md: backend/CLAUDE.md を参照
ドキュメント: docs/04_API_SPECIFICATION.md を参照

命名規約:
  - DB (SQLModel): snake_case (news_article_id)
  - API レスポンス (Pydantic): camelCase (newsArticleId)
  - TypeScript: camelCase (同上)
  → Pydantic の model_config で alias_generator = to_camel を設定

成果物:
  - Pydantic リクエスト/レスポンスモデル (backend/app/schemas/)
  - FastAPI 起動時に /openapi.json を自動生成
  - `npm run generate-types` で frontend/src/types/generated.ts を生成
検証:
  - Pydantic モデルの全フィールドが /openapi.json に存在
  - TypeScript 型が正しく生成される
```

### ステップ 5: バックエンドAPI実装 [サブエージェント: backend-api] ✅
```
対象: backend/app/routers/, backend/app/dependencies.py, backend/app/main.py
CLAUDE.md: backend/CLAUDE.md を参照
ドキュメント: docs/04_API_SPECIFICATION.md を参照
依存: Step 3, 4

成果物:
  - routers/news.py (一覧・詳細・手動フェッチ)
  - routers/keywords.py (CRUD)
  - dependencies.py (DIコンテナ)
  - main.py (ルーター登録、CORS、ライフサイクル)
検証:
  - Swagger UI から全エンドポイントが叩ける
  - キーワードのCRUDが動く
```

### ステップ 6: ニュース取得サービス [サブエージェント: fetcher] ✅
```
対象: backend/app/services/news_fetcher.py
CLAUDE.md: backend/CLAUDE.md を参照
依存: Step 3, 5

成果物:
  - news_fetcher.py (RSS取得、重複チェック、DB保存)
  - tests/test_news_fetcher.py
検証:
  - "Quantum Computing" でフェッチ → DBに保存
  - URL重複 → スキップ
  - 複数キーワード → 中間テーブル正しくリンク
```

### ステップ 7: AI分析サービス [サブエージェント: ai-service] ✅
```
対象: backend/app/services/ai_analyzer.py, gemini_analyzer.py
CLAUDE.md: backend/CLAUDE.md を参照
依存: Step 3

成果物:
  - ai_analyzer.py (BaseAnalyzer 抽象クラス)
  - gemini_analyzer.py (Gemini API 実装, gemini-2.5-flash-lite)
  - tests/test_ai_analyzer.py (モックAPI)
検証:
  - 英語記事 → 日本語翻訳・要約・センチメント
  - 不正JSON → エラーハンドリング
  - API失敗 → リトライ
```

### ステップ 8: タスクキュー [サブエージェント: task-queue] ✅
```
対象: backend/app/tasks/taskiq_worker.py
CLAUDE.md: backend/CLAUDE.md を参照
依存: Step 6, 7

成果物:
  - taskiq_worker.py (taskiq broker + scheduler + 5フェーズパイプライン)
  - docker-compose.yml に worker, scheduler, redis サービス追加
  - tests/test_taskiq_worker.py
検証:
  - worker コンテナ起動 → 定期実行でニュース取得・分析
  - ログに件数出力
```

### ステップ 9: フロントエンド実装 [サブエージェント: frontend] ✅
```
対象: frontend/src/
CLAUDE.md: frontend/CLAUDE.md を参照
ドキュメント: docs/04_API_SPECIFICATION.md を参照

成果物:
  - ダッシュボード (app/(protected)/page.tsx)
  - キーワード設定 (app/(protected)/settings/page.tsx)
  - ニュース詳細 (app/(protected)/news/[id]/page.tsx)
  - 各コンポーネント
  - lib/api-client.ts (サーバーサイドAPIクライアント)
検証:
  - ダッシュボード表示
  - キーワード追加・削除
  - フィルター・ソート
```

### ステップ 10: 統合 & E2E [メインエージェント] ✅
```
目的: 全体結合確認

実施:
  1. docker compose up で全サービス起動
  2. E2Eフロー:
     キーワード追加 → ニュース取得 → AI分析 → 画面表示
  3. エラーケース確認
```

## Phase 2 タスク分解（完了）

### Step A: 認証 (Better Auth BFF) ✅

初期実装は NextAuth.js + JWT だったが、Better Auth + BFF プロキシ構成に移行済み。

```
バックエンド:
  - dependencies.py: CurrentUser dataclass + BFF ヘッダー認証 (X-User-ID, X-Internal-Secret)
  - get_current_user, get_admin_user, get_optional_user
  - 削除: models/user.py, models/refresh_token.py, routers/auth.py, services/auth_service.py, schemas/auth.py
  - user_keyword.py, watchlist.py の user_id を INT → VARCHAR(32) に変更

フロントエンド:
  - lib/auth.ts (Better Auth サーバー設定: betterAuth())
  - lib/auth-client.ts (Better Auth クライアント: createAuthClient())
  - app/api/auth/[...all]/route.ts (Better Auth ハンドラ)
  - app/api/proxy/[...path]/route.ts (BFF プロキシ: セッション検証 + ヘッダー付与)
  - proxy.ts (CSP + Cookie 認証チェック)
  - components/auth/ (LoginForm, RegisterForm, AuthErrorWatcher)
  - 削除: SessionProvider.tsx, pages/api/auth/[...nextauth].ts, types/next-auth.d.ts

DB:
  - auth スキーマ (Better Auth CLI で管理)
  - Alembic: b1_better_auth_schema_migration (users/refresh_tokens 削除, user_id 型変更)
```

### Step B: ユーザー機能 (watchlist) ✅

※ subscriptions 機能は Phase 2.5 で撤去済み（user_keyword_subscriptions テーブル → レガシー）

```
バックエンド:
  - models/watchlist.py (WatchlistEntry, 複合PK: user_id + news_article_id)
  - schemas/user.py (WatchlistCreate/Response)
  - routers/me.py (watchlist CRUD)

フロントエンド:
  - lib/client-api.ts (クライアントサイドAPI, セッションデデュプリケーション)
  - components/news/WatchlistButton.tsx
  - app/(protected)/watchlist/page.tsx

削除済み:
  - models/user_keyword.py (UserKeywordSubscription)
  - components/keywords/SubscriptionToggle.tsx

Alembic:
  - dc3cc7a3c587: user_keyword_subscriptions, watchlists テーブル（初期）
  - Phase 6b: watchlists → watchlist_entries（複合PK、user_id: UUID）
```

### Step C: 記事全文取得 (content_extractor) ✅
```
バックエンド:
  - services/content_extractor.py (trafilatura, robots.txtチェック, ドメイン別レート制限)
  - news_articles に content, content_fetched_at カラム追加
  - tests/test_content_extractor.py

フロントエンド:
  - NewsDetail に全文表示セクション追加

Alembic:
  - 3a9bf03a0b5f: content, content_fetched_at カラム追加
```

### Step D: タスクキュー (taskiq + Redis) ✅
```
バックエンド:
  - tasks/taskiq_worker.py (broker, scheduler, fetch_and_analyze_task)
  - 5フェーズパイプライン:
    1. アクティブキーワード読み込み
    2. RSS取得 (news_fetcher)
    3. 全文取得 (content_extractor)
    4. AI分析 (ai_analyzer)
    5. Embedding生成 (embedding)
  - APScheduler (scheduler.py) を完全撤去
  - tests/test_taskiq_worker.py

インフラ:
  - docker-compose.yml に redis, worker, scheduler サービス追加
```

### Step E: pgvector セマンティック検索 ✅
```
バックエンド:
  - services/embedding.py (BaseEmbedder, 適応的スロットリング, サーキットブレーカー)
  - services/gemini_embedder.py (GeminiEmbedder, gemini-embedding-001, 768次元)
  - routers/news.py に GET /{id}/similar, POST /embed 追加
  - news_articles に embedding vector(768) カラム + HNSWインデックス
  - tests/test_embedding.py

フロントエンド:
  - components/news/RelatedArticles.tsx

インフラ:
  - DBイメージを pgvector/pgvector:pg16 に変更

Alembic:
  - 4bf262125474: pgvector拡張 + embedding カラム + HNSWインデックス
```

## Phase 2.5 フロー図（進行中）

```
Step F: DB再設計 Phase 0-2                  ✅
  (カテゴリ統合、キーワード刷新)
  ↓
Step G: DB再設計 Phase 3                    ✅
  (news_sources テーブル再設計)
  ↓
Step H: DB再設計 Phase 4                    ✅
  (news_articles + article_analyses 分離)
  ↓
Step I: Better Auth UUID移行 (Phase 6a)     ✅ (コード完了、マイグレーション未実行)
  ↓
Step J: watchlist_entries (Phase 6b)        ✅ (コード完了、マイグレーション未実行)
  ↓
Step K: Next.js 15 → 16 + ESLint → Biome   ✅
  ↓
Step L: DDD 値オブジェクト + XSS対策        ✅
```

## Phase 2.5 タスク分解（進行中）

### Step F: DB再設計 Phase 0-2（カテゴリ統合・キーワード刷新）✅
```
対象: backend/app/models/, backend/alembic/
ドキュメント: specs/db-redesign.md, docs/02_DATABASE_DESIGN.md

成果物:
  - models/category.py: Category (投資カテゴリ + キーワードカテゴリ統合)
  - models/keyword.py: Keyword (status, is_ai_generated 追加)
  - models/associations.py: ArticleKeyword (複合PK)
  - 削除: models/investment_category.py, models/keyword_category.py

レガシー化:
  - investment_categories, keyword_categories テーブル（Phase 5 で DROP 予定）

Alembic:
  - c1: categories 統合テーブル作成
  - c2: keywords テーブル刷新
```

### Step G: DB再設計 Phase 3（news_sources テーブル再設計）✅
```
対象: backend/app/models/news_source.py, backend/app/services/source_helpers.py

成果物:
  - models/news_source.py: NewsSource (source_type, site_url, endpoint_url, is_active)
  - services/source_helpers.py: ソース共通ヘルパー
  - schemas/news_source.py: NewsSourceCreate/Update/Response
  - routers/news_sources.py: /api/v1/sources (CRUD, admin限定)

Alembic:
  - c3: news_sources テーブル再設計
```

### Step H: DB再設計 Phase 4（news_articles + article_analyses 分離）✅
```
対象: backend/app/models/news.py, backend/app/models/analysis.py
ドキュメント: plans/migration/phase4-news-articles-and-article-analyses.md

成果物:
  - models/news.py: NewsArticle (original_title, original_url, news_source_id, article_group_id)
  - models/analysis.py: ArticleAnalysis (1:1 分離、translated_title_ja, summary_ja, impact_level)
  - models/article_group.py: ArticleGroup (重複記事グループ)
  - models/fetch_log.py: FetchLog
  - 全ルーター・サービス・フロントエンドのコード切替完了

レガシー化:
  - 旧カラム (title_original, url, source 等) は Phase 5 で DROP 予定

Alembic:
  - c4: news_articles 新カラム追加
  - c5: article_analyses テーブル作成
  - c6: article_groups テーブル作成
  - c7: fetch_logs テーブル作成
```

### Step I: Better Auth UUID移行（Phase 6a）✅
```
対象: backend/app/models/, backend/app/dependencies.py
ドキュメント: plans/migration/phase6a-uuid-migration.md

成果物:
  - models/auth_ref.py: AuthUserRef (Better Auth user FK参照)
  - dependencies.py: user_id を UUID 型で扱う
  - watchlist_entries, keyword テーブルの user_id を UUID に変更

ステータス: コード完了、DBマイグレーション未実行
```

### Step J: watchlist_entries 複合PK（Phase 6b）✅
```
対象: backend/app/models/watchlist.py, backend/app/schemas/user.py
ドキュメント: plans/migration/phase6b-watchlist-entries.md

成果物:
  - models/watchlist.py: WatchlistEntry (user_id: UUID + news_article_id 複合PK)
  - schemas/user.py: WatchlistCreate/Response (UUID対応)
  - routers/me.py: watchlist CRUD (複合PK対応)

ステータス: コード完了、DBマイグレーション未実行
```

### Step K: Next.js 15 → 16 + ESLint → Biome ✅
```
対象: frontend/

成果物:
  - Next.js 16 (App Router) にアップグレード
  - ESLint を完全撤去、Biome に移行
  - biome.json: lint/format 設定
  - package.json: 依存関係更新

削除:
  - .eslintrc.json, eslint 関連パッケージ
```

### Step L: DDD 値オブジェクト + XSS対策 ✅
```
対象: backend/app/domain/, frontend/src/proxy.ts

成果物:
  - domain/category.py: CategorySlug, CategoryName 値オブジェクト
  - domain/keyword.py: KeywordName 値オブジェクト
  - utils/sanitize.py: XSSサニタイズ (bleach)
  - utils/redis_cache.py: Redisキャッシュヘルパー
  - proxy.ts: CSP nonce ベースヘッダー + セキュリティヘッダー
```

## エラー発生時のフロー

サブエージェントがエラーに遭遇した場合:
1. エラーログを収集
2. 関連するCLAUDE.mdの記述を再確認
3. 依存モジュールのインターフェースを確認
4. 解決できない場合はメインエージェントにエスカレーション

メインエージェントの対応:
1. エラーの影響範囲を特定
2. 必要であれば Pydantic schemas（SSoT）を修正
3. 影響を受ける他のサブエージェントに変更を通知
