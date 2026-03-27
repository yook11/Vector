# ディレクトリ構成

## ツリー

```
Vector/
├── CLAUDE.md                          # ルート: プロジェクト全体のルール
├── docker-compose.yml                 # 全サービス定義 (frontend, backend, db, redis, worker, scheduler)
├── .env.example
├── domain.md                          # ドメインモデル概要
├── Domain_inventory.md                # ドメインインベントリ
│
├── frontend/
│   ├── CLAUDE.md                      # フロントエンド固有のルール
│   ├── Dockerfile
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── next.config.js
│   ├── postcss.config.js
│   ├── biome.json                    # Biome lint/format設定
│   ├── components.json                # shadcn/ui設定
│   ├── openapi.json                   # キャッシュ済みOpenAPIスキーマ（generate-types:file用）
│   └── src/
│       ├── proxy.ts                   # CSPヘッダ（nonce）+ Better Auth cookie認証チェック
│       ├── globals.css
│       ├── app/
│       │   ├── layout.tsx             # ルートレイアウト (Toaster)
│       │   ├── not-found.tsx
│       │   ├── api/
│       │   │   ├── auth/
│       │   │   │   └── [...all]/
│       │   │   │       └── route.ts   # Better Auth ハンドラ
│       │   │   └── proxy/
│       │   │       └── [...path]/
│       │   │           └── route.ts   # BFF プロキシ (→ FastAPI)
│       │   ├── auth/
│       │   │   ├── login/
│       │   │   │   └── page.tsx       # ログイン画面
│       │   │   └── register/
│       │   │       └── page.tsx       # ユーザー登録画面
│       │   └── (protected)/           # 認証必須ルートグループ
│       │       ├── layout.tsx         # Header含むレイアウト (Better Auth session)
│       │       ├── loading.tsx
│       │       ├── page.tsx           # ダッシュボード（ニュース一覧）
│       │       ├── settings/
│       │       │   ├── page.tsx       # キーワード・ソース設定画面
│       │       │   └── loading.tsx
│       │       ├── news/
│       │       │   └── [id]/
│       │       │       ├── page.tsx   # ニュース詳細 + 関連記事
│       │       │       └── not-found.tsx
│       │       └── watchlist/
│       │           └── page.tsx       # ウォッチリスト画面
│       ├── components/
│       │   ├── auth/
│       │   │   ├── AuthErrorWatcher.tsx  # セッションエラー監視 (useSession)
│       │   │   ├── LoginForm.tsx         # signIn.email()
│       │   │   └── RegisterForm.tsx      # signUp.email()
│       │   ├── layout/
│       │   │   ├── Header.tsx
│       │   │   ├── Sidebar.tsx
│       │   │   ├── CategorySidebar.tsx
│       │   │   ├── MobileSidebar.tsx
│       │   │   ├── ThemeToggle.tsx
│       │   │   └── UserMenu.tsx          # useSession / signOut
│       │   ├── news/
│       │   │   ├── CategoryBadge.tsx
│       │   │   ├── DuplicateBadge.tsx     # 重複記事グループ表示
│       │   │   ├── FetchButton.tsx        # 手動RSSフェッチトリガー
│       │   │   ├── NewsCard.tsx
│       │   │   ├── NewsDetail.tsx
│       │   │   ├── NewsFilters.tsx
│       │   │   ├── NewsList.tsx
│       │   │   ├── NewsPagination.tsx
│       │   │   ├── RelatedArticles.tsx    # pgvector類似記事表示
│       │   │   ├── SearchBar.tsx
│       │   │   └── WatchlistButton.tsx
│       │   ├── keywords/
│       │   │   ├── AddKeywordDialog.tsx
│       │   │   ├── KeywordRow.tsx
│       │   │   ├── KeywordTable.tsx
│       │   │   └── KeywordTag.tsx
│       │   ├── sources/
│       │   │   ├── SourceFormDialog.tsx   # ソース作成/編集ダイアログ
│       │   │   ├── SourceManager.tsx      # ソース管理コンテナ
│       │   │   └── SourceTable.tsx        # ソース一覧テーブル
│       │   └── ui/                    # shadcn/ui（自動生成、手動編集禁止）
│       │       ├── alert-dialog.tsx
│       │       ├── badge.tsx
│       │       ├── button.tsx
│       │       ├── card.tsx
│       │       ├── dialog.tsx
│       │       ├── input.tsx
│       │       ├── label.tsx
│       │       ├── select.tsx
│       │       ├── separator.tsx
│       │       ├── sheet.tsx
│       │       ├── skeleton.tsx
│       │       ├── sonner.tsx
│       │       ├── switch.tsx
│       │       └── table.tsx
│       ├── lib/
│       │   ├── api-client.ts          # サーバーサイドAPIクライアント（SSR用、BFFヘッダー付与）
│       │   ├── auth.ts                # Better Auth サーバー設定 (betterAuth())
│       │   ├── auth-client.ts         # Better Auth クライアント (createAuthClient())
│       │   ├── client-api.ts          # クライアントサイドAPIクライアント（/api/proxy経由）
│       │   └── utils.ts              # cn() ユーティリティ
│       └── types/
│           ├── generated.ts           # OpenAPIから自動生成（手動編集禁止）
│           └── index.ts               # re-export + narrowing
│
├── backend/
│   ├── CLAUDE.md                      # バックエンド固有のルール
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py                    # FastAPIエントリーポイント（SecurityHeaders, CORS）
│   │   ├── config.py                  # 環境変数管理 (pydantic-settings)
│   │   ├── db.py                      # DB接続・セッション管理
│   │   ├── dependencies.py            # FastAPI DI (get_session, get_current_user, get_admin_user, get_optional_user)
│   │   ├── domain/                    # DDD 値オブジェクト
│   │   │   ├── category.py           # CategorySlug, CategoryName
│   │   │   └── keyword.py            # KeywordName
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── news.py               # NewsArticle (original_title, original_url, embedding, article_group_id)
│   │   │   ├── analysis.py           # ArticleAnalysis (translated_title_ja, summary_ja, impact_level)
│   │   │   ├── category.py           # Category (統合済み: 旧 investment_category + keyword_category)
│   │   │   ├── keyword.py            # Keyword
│   │   │   ├── associations.py       # ArticleKeyword
│   │   │   ├── ai_model.py           # AIModel
│   │   │   ├── article_group.py      # ArticleGroup (重複記事グループ)
│   │   │   ├── auth_ref.py           # AuthUserRef (Better Auth user FK参照)
│   │   │   ├── fetch_log.py          # FetchLog
│   │   │   ├── news_source.py        # NewsSource, SourceType
│   │   │   └── watchlist.py          # WatchlistEntry (user_id: UUID, 複合PK)
│   │   ├── schemas/                   # Pydantic schemas（SSoT: 型の源泉）
│   │   │   ├── __init__.py
│   │   │   ├── news.py               # NewsResponse, PaginatedNewsResponse, NewsFetchRequest/Response
│   │   │   ├── analysis.py           # AnalysisResponse, AIModelBrief
│   │   │   ├── category.py           # CategoryResponse/ListResponse/Brief
│   │   │   ├── keyword.py            # KeywordCreate/Update/Response/ListResponse, KeywordBrief
│   │   │   ├── news_source.py        # NewsSourceCreate/Update/Response/ListResponse
│   │   │   └── user.py               # SubscriptionCreate/Response, WatchlistCreate/Response
│   │   ├── routers/
│   │   │   ├── __init__.py
│   │   │   ├── news.py               # /api/v1/news (一覧・詳細・フェッチ・embed・similar・groups)
│   │   │   ├── keywords.py           # /api/v1/keywords (CRUD)
│   │   │   ├── me.py                 # /api/v1/me (subscriptions, watchlist)
│   │   │   ├── news_sources.py       # /api/v1/sources (CRUD, admin限定)
│   │   │   └── categories.py         # /api/v1/categories (統合カテゴリ一覧)
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── news_fetcher.py       # RSS取得・重複チェック・DB保存
│   │   │   ├── ai_analyzer.py        # BaseAnalyzer抽象クラス + 分析オーケストレーション
│   │   │   ├── gemini_analyzer.py    # GeminiAnalyzer (gemini-2.5-flash-lite)
│   │   │   ├── content_extractor.py  # 記事全文取得 (trafilatura)
│   │   │   ├── embedding.py          # BaseEmbedder抽象クラス + embeddingオーケストレーション
│   │   │   ├── gemini_embedder.py    # GeminiEmbedder (768次元)
│   │   │   ├── dedup.py              # 重複記事検出・グループ化 (cosine distance)
│   │   │   ├── hacker_news.py        # Hacker News API フェッチャー
│   │   │   ├── alpha_vantage.py      # Alpha Vantage ニュースAPI フェッチャー
│   │   │   └── source_helpers.py     # ニュースソース共通ヘルパー
│   │   ├── tasks/
│   │   │   ├── __init__.py
│   │   │   └── taskiq_worker.py      # taskiqブローカー・スケジューラー・パイプライン
│   │   ├── scripts/
│   │   │   └── compare_models.py     # AIモデル比較ツール
│   │   └── utils/
│   │       ├── sanitize.py           # XSSサニタイズ (bleach)
│   │       └── redis_cache.py        # Redisキャッシュヘルパー
│   ├── alembic/
│   │   ├── env.py                    # authスキーマをautogenerateから除外
│   │   └── versions/                 # マイグレーション履歴（31件）
│   └── tests/
│       ├── CLAUDE.md                  # テストの書き方ルール
│       ├── conftest.py               # フィクスチャ (db_session, client, BFFヘッダー認証)
│       ├── test_news_fetcher.py
│       ├── test_ai_analyzer.py
│       ├── test_content_extractor.py
│       ├── test_embedding.py
│       ├── test_dedup.py
│       ├── test_semantic_search.py
│       ├── test_hacker_news.py
│       ├── test_alpha_vantage.py
│       ├── test_fetch_logs.py
│       ├── test_taskiq_worker.py
│       ├── test_routers/
│       │   ├── test_news.py
│       │   ├── test_keywords.py
│       │   ├── test_me.py
│       │   ├── test_news_sources.py
│       │   └── test_categories.py
│       └── test_domain/
│           └── test_category_values.py  # 値オブジェクトテスト
│
├── docs/                              # 設計ドキュメント
│   ├── 00_PROJECT_OVERVIEW.md
│   ├── 01_DIRECTORY_STRUCTURE.md       # このファイル
│   ├── 02_DATABASE_DESIGN.md
│   ├── archive/
│   │   └── 03_CLAUDE_CODE_WORKFLOW.md  # アーカイブ済み
│   ├── 04_API_SPECIFICATION.md
│   ├── 05_PHASE2_PLAN.md
│   ├── 05b_TASKQUEUE_POC_REPORT.md
│   └── 06_PROMPT_DESIGN.md
│
├── specs/                             # 設計仕様書
│   ├── design-principles.md           # DDD・セキュア・バイ・デザイン設計原則
│   ├── use-cases.md                   # ユースケース定義
│   ├── db-redesign.md/               # DBスキーマ再設計仕様（テーブル別詳細）
│   └── domain-analysis.md/           # ドメイン分析（エンティティ関係・不変条件）
│
└── plans/                             # マイグレーション計画
    ├── migration/                     # アクティブな移行計画
    │   ├── phase4-news-articles-and-article-analyses.md
    │   ├── phase6a-uuid-migration.md
    │   └── phase6b-watchlist-entries.md
    └── archived/                      # 完了済み計画（参照用）
```

## 型パイプライン（SSoT → フロント型生成）

```
backend/app/schemas/ (Pydantic, SSoT)
  ↓ FastAPI が自動生成
/openapi.json
  ↓ npm run generate-types
frontend/src/types/generated.ts（手動編集禁止）
  ↓ re-export + narrowing
frontend/src/types/index.ts
```

## 認証アーキテクチャ

```
Browser (Cookie: better-auth.session_token)
  │
  ├─► /api/auth/* → Better Auth Server (frontend/src/lib/auth.ts)
  │     └── PostgreSQL auth スキーマ (user, session, account, verification)
  │
  └─► /api/proxy/* → BFF Proxy (frontend/src/app/api/proxy/[...path]/route.ts)
        ├── Cookie → Better Auth session 検証
        ├── ヘッダー付与: X-User-ID, X-User-Role, X-Internal-Secret
        └── → FastAPI Backend (INTERNAL_API_URL)
              └── dependencies.py: get_current_user() でヘッダー検証
```

## CLAUDE.md 配置と対象サブエージェント

| ファイル | 対象サブエージェント | 主な内容 |
|---------|-------------------|---------|
| `/CLAUDE.md` | メインエージェント | プロジェクト全体のルール、命名規則、コミット規約 |
| `/frontend/CLAUDE.md` | フロントエンド担当 | Next.js規約、コンポーネント設計、スタイリングルール |
| `/backend/CLAUDE.md` | バックエンド担当 | FastAPI規約、DB操作、サービス層の設計指針 |
| `/backend/tests/CLAUDE.md` | テスト担当 | テストの書き方、フィクスチャ、モック方針 |

## サブエージェント分担の境界

```
メインエージェント（オーケストレーター）
├── サブエージェント A: backend/app/schemas → Pydanticスキーマ定義（SSoT）
├── サブエージェント B: frontend/ → Next.js UI実装
├── サブエージェント C: backend/routers → API実装
├── サブエージェント D: backend/services + tasks → ビジネスロジック・タスクキュー
├── サブエージェント E: backend/models + alembic → DB設計・マイグレーション
└── サブエージェント F: docker + CI → インフラ・デプロイ
```
