# ディレクトリ構成

## ツリー

```
Vector/
├── CLAUDE.md                          # ルート: プロジェクト全体のルール
├── docker-compose.yml
├── .env.example
├── .github/
│   └── workflows/
│       ├── ci-frontend.yml
│       └── ci-backend.yml
│
├── shared/                            # フロント・バック共有の型定義
│   ├── CLAUDE.md                      # 共有型の管理ルール
│   └── api-schema/
│       ├── openapi.yaml               # APIスキーマ（Single Source of Truth）
│       └── types.ts                   # OpenAPIから自動生成されるTS型
│
├── frontend/
│   ├── CLAUDE.md                      # フロントエンド固有のルール
│   ├── Dockerfile
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── next.config.js
│   ├── components.json                # shadcn/ui設定
│   └── src/
│       ├── app/
│       │   ├── layout.tsx
│       │   ├── page.tsx               # ダッシュボード
│       │   ├── settings/
│       │   │   └── page.tsx           # キーワード設定画面
│       │   ├── news/
│       │   │   └── [id]/
│       │   │       └── page.tsx       # ニュース詳細画面
│       │   └── api/
│       │       └── mock/              # モックAPI（開発用）
│       │           ├── news/
│       │           │   └── route.ts
│       │           └── keywords/
│       │               └── route.ts
│       ├── components/
│       │   ├── layout/
│       │   │   ├── Header.tsx
│       │   │   ├── Sidebar.tsx
│       │   │   └── Footer.tsx
│       │   ├── news/
│       │   │   ├── NewsCard.tsx
│       │   │   ├── NewsList.tsx
│       │   │   ├── NewsDetail.tsx
│       │   │   └── SentimentBadge.tsx
│       │   ├── keywords/
│       │   │   ├── KeywordManager.tsx
│       │   │   └── KeywordTag.tsx
│       │   └── ui/                    # shadcn/ui（自動生成）
│       ├── lib/
│       │   ├── api-client.ts          # API呼び出し（型安全）
│       │   └── utils.ts
│       ├── hooks/
│       │   ├── useNews.ts
│       │   └── useKeywords.ts
│       └── types/
│           └── index.ts               # shared/api-schemaから再エクスポート
│
├── backend/
│   ├── CLAUDE.md                      # バックエンド固有のルール
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py                    # FastAPIエントリーポイント
│   │   ├── config.py                  # 環境変数管理
│   │   ├── db.py                      # DB接続・セッション管理
│   │   ├── dependencies.py            # FastAPI DI
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── news.py
│   │   │   ├── keyword.py
│   │   │   ├── analysis.py
│   │   │   └── associations.py
│   │   ├── schemas/
│   │   │   ├── __init__.py
│   │   │   ├── news.py
│   │   │   ├── keyword.py
│   │   │   └── analysis.py
│   │   ├── routers/
│   │   │   ├── __init__.py
│   │   │   ├── news.py
│   │   │   └── keywords.py
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── news_fetcher.py
│   │   │   ├── ai_analyzer.py
│   │   │   ├── gemini_analyzer.py
│   │   │   ├── openai_analyzer.py     # 将来用
│   │   │   └── scheduler.py
│   │   └── utils/
│   │       ├── __init__.py
│   │       └── logger.py
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   └── tests/
│       ├── CLAUDE.md                  # テストの書き方ルール
│       ├── conftest.py
│       ├── test_news_fetcher.py
│       ├── test_ai_analyzer.py
│       └── test_routers/
│           ├── test_news.py
│           └── test_keywords.py
│
└── docs/
    ├── 00_PROJECT_OVERVIEW.md
    ├── 01_DIRECTORY_STRUCTURE.md       # このファイル
    ├── 02_DATABASE_DESIGN.md
    ├── 03_CLAUDE_CODE_WORKFLOW.md
    └── 04_API_SPECIFICATION.md
```

## CLAUDE.md 配置と対象サブエージェント

| ファイル | 対象サブエージェント | 主な内容 |
|---------|-------------------|---------|
| `/CLAUDE.md` | メインエージェント | プロジェクト全体のルール、命名規則、コミット規約 |
| `/shared/CLAUDE.md` | 型定義担当 | APIスキーマの管理方法、型の同期ルール |
| `/frontend/CLAUDE.md` | フロントエンド担当 | Next.js規約、コンポーネント設計、スタイリングルール |
| `/backend/CLAUDE.md` | バックエンド担当 | FastAPI規約、DB操作、サービス層の設計指針 |
| `/backend/tests/CLAUDE.md` | テスト担当 | テストの書き方、フィクスチャ、モック方針 |

## サブエージェント分担の境界

```
メインエージェント（オーケストレーター）
├── サブエージェント A: shared/api-schema → OpenAPIスキーマ定義
├── サブエージェント B: frontend/ → Next.js UI実装
├── サブエージェント C: backend/routers + schemas → API実装
├── サブエージェント D: backend/services → ビジネスロジック
├── サブエージェント E: backend/models + alembic → DB設計・マイグレーション
└── サブエージェント F: docker + CI → インフラ・デプロイ
```
