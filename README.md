# Vector

> 海外テックニュース収集・AI翻訳・投資分析ダッシュボード

次世代コンピューティング、マテリアル・インフォマティクスなど、
日本では情報が少ない先端分野の海外ニュースを自動収集し、
AIで翻訳・要約・センチメント分析を行う投資ダッシュボード。

## Tech Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Frontend | Next.js 16 (App Router) + TypeScript | Tailwind CSS + shadcn/ui + Biome |
| Backend | FastAPI + Python 3.12 + SQLModel | 非同期処理、Pydantic v2 |
| Auth | Better Auth (BFF Proxy) | Cookie ベースセッション + 内部 API ヘッダー認証 |
| Database | PostgreSQL 16 + pgvector | Alembic マイグレーション管理 |
| AI | Gemini API | 翻訳・要約・センチメント分析・Embedding |
| Task Queue | taskiq + Redis | 定期実行・非同期タスク処理 |
| CI/CD | GitHub Actions | lint + test + type check |
| Infrastructure | Docker Compose | 6 サービス構成 |

## Prerequisites

- Docker & Docker Compose
- Gemini API Key ([Google AI Studio](https://aistudio.google.com/) で取得)

## Getting Started

```bash
# 1. Clone & setup
cp .env.example .env
# .env の GEMINI_API_KEY を設定

# 2. Start all services
docker compose up --build

# 3. Access
# Frontend: http://localhost:3000
# Database: localhost:5433 (user: vector, password: vector)
```

> **Note**: バックエンドAPIは Docker 内部ネットワークのみで公開されます。
> フロントエンド (BFF) がプロキシとして機能するため、ブラウザからは `localhost:3000` のみにアクセスします。

### 初回利用の流れ

1. `http://localhost:3000/auth/register` でアカウント登録
2. Settings (`/settings`) でキーワードを追加（例: "Quantum Computing"）
3. ダッシュボードで「Fetch News」ボタンをクリック
4. スケジューラーが自動でAI分析を実行（間隔: `FETCH_INTERVAL_HOURS`）
5. ダッシュボードをリロードして翻訳・分析結果を確認

## Architecture

```
Browser
  │
  └─► Next.js Frontend / BFF (localhost:3000)
        ├── Better Auth (Cookie-based session, auth schema in PG)
        ├── Server Components → INTERNAL_API_URL (Docker internal)
        ├── BFF Proxy (/api/proxy/*) → Backend (header auth)
        │
        └─► FastAPI Backend (Docker internal only)
              ├── Header Auth (X-Internal-User-Id / X-Internal-Secret)
              ├── News Fetcher (Google News RSS, Hacker News API, Alpha Vantage)
              ├── AI Analyzer (Gemini API — 翻訳・要約・センチメント)
              ├── Embedding (Gemini Embedding API — pgvector)
              └── PostgreSQL 16 + pgvector

Redis ◄── taskiq worker (非同期タスク実行)
       ◄── taskiq scheduler (cron トリガー)
```

### Docker Compose サービス一覧

| Service | Description |
|---------|------------|
| `frontend` | Next.js 16 BFF — 唯一の public エントリーポイント |
| `backend` | FastAPI — 内部ネットワークのみ |
| `db` | PostgreSQL 16 + pgvector |
| `redis` | タスクキューブローカー |
| `worker` | taskiq ワーカー（ニュースパイプライン実行） |
| `scheduler` | taskiq スケジューラー（cron トリガー） |

## Environment Variables

`.env.example` を参照。主要な変数:

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://vector:vector@db:5432/vector` | Backend DB接続 |
| `AUTH_DATABASE_URL` | `postgresql://vector:vector@db:5432/vector?search_path=auth` | Better Auth 用 (auth スキーマ) |

### AI

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Google AI API Key (必須) |
| `AI_PROVIDER` | `gemini` | AI プロバイダー |

### Auth

| Variable | Default | Description |
|----------|---------|-------------|
| `BETTER_AUTH_SECRET` | dev default | Better Auth セッション暗号化キー |
| `BETTER_AUTH_URL` | `http://localhost:3000` | Better Auth コールバックURL |
| `INTERNAL_API_SECRET` | dev default | BFF→Backend 内部通信の認証シークレット |

### Task Queue

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 接続先 |
| `FETCH_INTERVAL_HOURS` | `12` | スケジューラー実行間隔 |
| `MAX_ARTICLES_PER_FETCH` | `50` | 1回のフェッチ上限 |

### App URLs

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTEND_URL` | `http://localhost:3000` | CORS許可オリジン |
| `INTERNAL_API_URL` | `http://backend:8000/api/v1` | SSR からの Backend 接続先 (Docker内部) |

## Development

### テスト・lint 実行

```bash
# Backend
docker compose exec backend ruff check app/
docker compose exec backend ruff format --check app/
docker compose exec backend python -m pytest tests/ -x -q

# Frontend
docker compose exec frontend npx biome check src/
docker compose exec frontend npx tsc --noEmit
```

### DB操作

```bash
# マイグレーション実行
docker compose exec backend alembic upgrade head

# ロールバック
docker compose exec backend alembic downgrade -1

# DB接続
docker compose exec db psql -U vector -d vector
```

### 型生成パイプライン

Backend の Pydantic schemas が SSoT (Single Source of Truth)。変更後は型を再生成する:

```bash
# Backend 起動中に実行
cd frontend && npm run generate-types
```

## Documentation

設計ドキュメントは `docs/` を参照:

- `docs/00_PROJECT_OVERVIEW.md` — プロジェクト概要
- `docs/01_DIRECTORY_STRUCTURE.md` — ディレクトリ構成
- `docs/02_DATABASE_DESIGN.md` — DB設計
- `docs/03_CLAUDE_CODE_WORKFLOW.md` — 開発ワークフロー
- `docs/04_API_SPECIFICATION.md` — API仕様
- `docs/05_PHASE2_PLAN.md` — Phase 2 計画
- `docs/05b_TASKQUEUE_POC_REPORT.md` — タスクキュー設計
