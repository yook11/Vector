# Vector

> 海外テックニュース収集・AI翻訳・投資分析ダッシュボード

次世代コンピューティング、マテリアル・インフォマティクスなど、
日本では情報が少ない先端分野の海外ニュースを自動収集し、
AIで翻訳・要約・センチメント分析を行う投資ダッシュボード。

## Tech Stack

- **Frontend**: Next.js 14 (App Router) + TypeScript + Tailwind CSS + shadcn/ui
- **Backend**: FastAPI + Python 3.12 + SQLModel
- **Database**: PostgreSQL 16 (Alembic migration)
- **AI**: Gemini API (抽象化済み、差し替え可能)
- **Auth**: JWT + Refresh Token Rotation (NextAuth v4)
- **Infrastructure**: Docker Compose

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
# Backend API docs: http://localhost:8000/docs
# Database: localhost:5433 (user: vector, password: vector)
```

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
  ├─► Next.js Frontend (localhost:3000)
  │     ├── Server Components → INTERNAL_API_URL (Docker内部)
  │     └── Client Components → NEXT_PUBLIC_API_URL (localhost:8000)
  │
  └─► FastAPI Backend (localhost:8000)
        ├── JWT Auth (access token 60min + refresh token 30days)
        ├── News Fetcher (Google News RSS)
        ├── AI Analyzer (Gemini API)
        ├── APScheduler (定期実行)
        └── PostgreSQL 16 (Docker内部)
```

## Environment Variables

### Backend (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://vector:vector@db:5432/vector` | DB接続文字列 |
| `GEMINI_API_KEY` | — | Google AI API Key (必須) |
| `AI_PROVIDER` | `gemini` | AI プロバイダー |
| `FETCH_INTERVAL_HOURS` | `3` | スケジューラー実行間隔 |
| `MAX_ARTICLES_PER_FETCH` | `50` | 1回のフェッチ上限 |
| `JWT_SECRET` | dev default | JWT署名キー (本番環境では必ず変更すること) |
| `FRONTEND_URL` | `http://localhost:3000` | CORS許可オリジン |

### Frontend (docker-compose.yml)

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000/api/v1` | ブラウザからのAPI接続先 |
| `INTERNAL_API_URL` | `http://backend:8000/api/v1` | SSR (Server Components) からのAPI接続先 (Docker内部DNS) |
| `NEXTAUTH_URL` | `http://localhost:3000` | NextAuth コールバックURL |
| `NEXTAUTH_SECRET` | dev default | NextAuth セッション暗号化キー (本番環境では必ず変更すること) |

## Development

### テスト実行

```bash
# Backend tests
docker compose exec backend pytest -v

# Frontend lint
docker compose exec frontend npx next lint
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

## Documentation

設計ドキュメントは `docs/` を参照:

- `docs/00_PROJECT_OVERVIEW.md` — プロジェクト概要
- `docs/01_DIRECTORY_STRUCTURE.md` — ディレクトリ構成
- `docs/02_DATABASE_DESIGN.md` — DB設計
- `docs/03_CLAUDE_CODE_WORKFLOW.md` — 開発ワークフロー
- `docs/04_API_SPECIFICATION.md` — API仕様
