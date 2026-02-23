# ディレクトリ構成

## ツリー

```
Vector/
├── CLAUDE.md                          # ルート: プロジェクト全体のルール
├── docker-compose.yml                 # 全サービス定義 (frontend, backend, db, redis, worker, scheduler)
├── .env.example
│
├── frontend/
│   ├── CLAUDE.md                      # フロントエンド固有のルール
│   ├── Dockerfile
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── next.config.js
│   ├── components.json                # shadcn/ui設定
│   ├── openapi.json                   # キャッシュ済みOpenAPIスキーマ（generate-types:file用）
│   └── src/
│       ├── middleware.ts              # NextAuth ルート保護
│       ├── app/
│       │   ├── layout.tsx             # ルートレイアウト (SessionProvider, Toaster)
│       │   ├── not-found.tsx
│       │   ├── auth/
│       │   │   ├── login/
│       │   │   │   └── page.tsx       # ログイン画面
│       │   │   └── register/
│       │   │       └── page.tsx       # ユーザー登録画面
│       │   └── (protected)/           # 認証必須ルートグループ
│       │       ├── layout.tsx         # Header含むレイアウト
│       │       ├── loading.tsx
│       │       ├── page.tsx           # ダッシュボード（ニュース一覧）
│       │       ├── settings/
│       │       │   ├── page.tsx       # キーワード設定画面
│       │       │   └── loading.tsx
│       │       ├── news/
│       │       │   └── [id]/
│       │       │       ├── page.tsx   # ニュース詳細 + 関連記事
│       │       │       └── not-found.tsx
│       │       └── watchlist/
│       │           └── page.tsx       # ウォッチリスト画面
│       ├── pages/
│       │   └── api/
│       │       └── auth/
│       │           └── [...nextauth].ts  # NextAuth APIルート
│       ├── components/
│       │   ├── auth/
│       │   │   ├── AuthErrorWatcher.tsx  # リフレッシュトークンエラー監視
│       │   │   ├── LoginForm.tsx
│       │   │   ├── RegisterForm.tsx
│       │   │   └── SessionProvider.tsx
│       │   ├── layout/
│       │   │   ├── Header.tsx
│       │   │   ├── Sidebar.tsx
│       │   │   ├── MobileSidebar.tsx
│       │   │   └── UserMenu.tsx
│       │   ├── news/
│       │   │   ├── FetchButton.tsx        # 手動RSSフェッチトリガー
│       │   │   ├── ImpactScore.tsx
│       │   │   ├── NewsCard.tsx
│       │   │   ├── NewsDetail.tsx
│       │   │   ├── NewsFilters.tsx
│       │   │   ├── NewsList.tsx
│       │   │   ├── NewsPagination.tsx
│       │   │   ├── RelatedArticles.tsx    # pgvector類似記事表示
│       │   │   ├── SentimentBadge.tsx
│       │   │   └── WatchlistButton.tsx
│       │   ├── keywords/
│       │   │   ├── AddKeywordDialog.tsx
│       │   │   ├── KeywordRow.tsx
│       │   │   ├── KeywordTable.tsx
│       │   │   ├── KeywordTag.tsx
│       │   │   └── SubscriptionToggle.tsx
│       │   └── ui/                    # shadcn/ui（自動生成、手動編集禁止）
│       ├── lib/
│       │   ├── api-client.ts          # サーバーサイドAPIクライアント（SSR用）
│       │   ├── client-api.ts          # クライアントサイドAPIクライアント（use client）
│       │   ├── auth.ts               # NextAuth設定（authOptions）
│       │   └── utils.ts              # cn() ユーティリティ
│       └── types/
│           ├── generated.ts           # OpenAPIから自動生成（手動編集禁止）
│           ├── index.ts               # re-export + narrowing
│           └── next-auth.d.ts         # NextAuth型拡張
│
├── backend/
│   ├── CLAUDE.md                      # バックエンド固有のルール
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py                    # FastAPIエントリーポイント
│   │   ├── config.py                  # 環境変数管理 (pydantic-settings)
│   │   ├── db.py                      # DB接続・セッション管理
│   │   ├── dependencies.py            # FastAPI DI (get_session, get_current_user, get_optional_user)
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── news.py               # NewsArticle (embedding含む)
│   │   │   ├── keyword.py            # Keyword
│   │   │   ├── analysis.py           # AnalysisResult
│   │   │   ├── associations.py       # NewsKeyword
│   │   │   ├── user.py               # User
│   │   │   ├── refresh_token.py      # RefreshToken
│   │   │   ├── user_keyword.py       # UserKeywordSubscription
│   │   │   └── watchlist.py          # WatchlistItem
│   │   ├── schemas/                   # Pydantic schemas（SSoT: 型の源泉）
│   │   │   ├── __init__.py
│   │   │   ├── news.py               # NewsResponse, PaginatedNewsResponse, NewsFetchRequest/Response, EmbedResponse
│   │   │   ├── keyword.py            # KeywordCreate/Update/Response/ListResponse, KeywordBrief
│   │   │   ├── analysis.py           # AnalysisResponse
│   │   │   ├── auth.py               # LoginRequest, RegisterRequest, TokenResponse, RefreshRequest, UserResponse
│   │   │   └── user.py               # SubscriptionCreate/Response/ListResponse, WatchlistCreate/Response/ListResponse
│   │   ├── routers/
│   │   │   ├── __init__.py
│   │   │   ├── news.py               # /api/v1/news (一覧・詳細・フェッチ・embed・similar)
│   │   │   ├── keywords.py           # /api/v1/keywords (CRUD)
│   │   │   ├── auth.py               # /api/v1/auth (register/login/refresh/logout)
│   │   │   └── me.py                 # /api/v1/me (subscriptions, watchlist)
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── news_fetcher.py       # RSS取得・重複チェック・DB保存
│   │   │   ├── ai_analyzer.py        # BaseAnalyzer抽象クラス + 分析オーケストレーション
│   │   │   ├── gemini_analyzer.py    # GeminiAnalyzer (gemini-2.5-flash)
│   │   │   ├── content_extractor.py  # 記事全文取得 (newspaper4k)
│   │   │   ├── embedding.py          # BaseEmbedder抽象クラス + embeddingオーケストレーション
│   │   │   ├── gemini_embedder.py    # GeminiEmbedder (gemini-embedding-001, 768次元)
│   │   │   └── auth_service.py       # 認証ロジック (JWT, パスワードハッシュ, トークンローテーション)
│   │   └── tasks/
│   │       ├── __init__.py
│   │       └── taskiq_worker.py      # taskiqブローカー・スケジューラー・5フェーズパイプライン
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/                 # マイグレーション履歴（7件）
│   └── tests/
│       ├── CLAUDE.md                  # テストの書き方ルール
│       ├── conftest.py               # フィクスチャ (db_session, client, test_user, auth_headers等)
│       ├── test_news_fetcher.py
│       ├── test_ai_analyzer.py
│       ├── test_content_extractor.py
│       ├── test_embedding.py
│       ├── test_taskiq_worker.py
│       └── test_routers/
│           ├── __init__.py
│           ├── test_news.py
│           ├── test_keywords.py
│           ├── test_auth.py
│           └── test_me.py
│
└── docs/
    ├── 00_PROJECT_OVERVIEW.md
    ├── 01_DIRECTORY_STRUCTURE.md       # このファイル
    ├── 02_DATABASE_DESIGN.md
    ├── 03_CLAUDE_CODE_WORKFLOW.md
    ├── 04_API_SPECIFICATION.md
    ├── 05_PHASE2_PLAN.md
    └── 05b_TASKQUEUE_POC_REPORT.md
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
