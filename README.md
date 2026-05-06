# Vector

> 海外テックニュース収集・AI翻訳・投資分析ダッシュボード

次世代コンピューティング、マテリアル・インフォマティクスなど、
日本では情報が少ない先端分野の海外ニュースを自動収集し、
AIで翻訳・要約・インパクト分析を行う投資ダッシュボード。

### 解決する課題

- 先端テックのニュースは英語記事が多く、日本語話者の投資家が情報を得るまでにタイムラグがある
- ニュースに登場する企業の関連情報を複数ソース横断で確認するのが手間
- それらを一つの場所でスピード感を持って確認できるようにする

### 主要機能

- テックニュースの自動収集（RSS / Hacker News API / Alpha Vantage）
- AI翻訳・要約・インパクト分析（Gemini API）
- セマンティック検索・類似記事推薦（pgvector）
- 重複記事の自動検出・グループ化
- ウォッチリスト（記事ブックマーク）
- キーワード・ニュースソース管理（管理者向け）
- カテゴリ別のニュースフィルタリング

## Tech Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Frontend | Next.js 16 (App Router) + TypeScript | Tailwind CSS + shadcn/ui + Biome |
| Backend | FastAPI + Python 3.13 + SQLAlchemy 2.0 | 非同期処理、Pydantic v2 |
| Auth | Better Auth (BFF Proxy) | Cookie ベースセッション + 内部 API ヘッダー認証 |
| Database | PostgreSQL 16 + pgvector | Alembic マイグレーション管理 |
| AI | Gemini API (gemini-2.5-flash-lite) | 翻訳・要約・インパクト分析・Embedding |
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
# Frontend: http://localhost:3000 (唯一の host-exposed エントリポイント)
```

> **Note**: backend / db / redis / worker / scheduler は全て Docker 内部
> ネットワーク (`internal: true`) のみで動作し host port を持ちません。
> フロントエンド (BFF) がプロキシとして機能するため、ブラウザからは
> `localhost:3000` のみにアクセスします。db に直接 psql したい場合は
> `docker compose exec db psql -U vector -d vector` を使ってください。

### 既存 dev 環境のアップデート

PR #321 (`fix(infra): db を internal network 限定にし host expose を完全遮断`)
以降、`db` container は `internal: true` の network のみに接続し host port
expose を持たない設定に変更されています。**それより前から dev 環境を持って
いる場合**、稼働中の `vector-db-1` container は古い設定で起動されたまま
(host port 5433 expose + public network 接続) になっているため、以下を
1 度だけ実行して container を recreate してください:

```bash
docker compose up -d --force-recreate db
```

確認: `docker compose ps db` の `Ports` 列が空 (host expose なし) になり、
`Networks` が `vector_internal` のみになっていれば正しく適用されています。

### 初回利用の流れ

1. `http://localhost:3000/auth/register` でアカウント登録
2. 管理者がキーワード・ニュースソースを設定（`/settings`）
3. ダッシュボードで「Fetch News」ボタンをクリック
4. taskiq ワーカーが自動でニュース取得・AI分析・Embedding生成を実行
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
              ├── Header Auth (X-User-ID / X-Internal-Secret)
              ├── News Fetcher (RSS Feeds, Hacker News API, Alpha Vantage)
              ├── AI Analyzer (Gemini API — 翻訳・要約・インパクト分析)
              ├── Embedding (Gemini Embedding API — pgvector)
              ├── Dedup (cosine distance — 重複記事グループ化)
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

## Domain Concepts

| 概念 | 説明 | モデル |
|------|------|--------|
| ニュース記事 | 収集・翻訳されたテックニュース | `NewsArticle` |
| AI分析結果 | AIによる翻訳・要約・インパクト分析 | `ArticleAnalysis` |
| キーワード | 記事のタギングに使用される検索キーワード | `Keyword` |
| カテゴリ | キーワードと記事を分類する統合カテゴリ | `Category` |
| ニュースソース | ニュースの収集元メディア・サイト | `NewsSource` |
| フェッチログ | ニュース取得の実行履歴 | `FetchLog` |
| ウォッチリスト | ユーザーの記事ブックマーク | `WatchlistEntry` |

### ニュース処理パイプライン

```
taskiq scheduler (cron)
  ↓
1. アクティブキーワード読み込み
  ↓
2. RSS / API フェッチ → NewsArticle 保存
  ↓
3. 全文取得 (trafilatura)
  ↓
4. AI分析 (Gemini API → ArticleAnalysis)
  ↓
5. Embedding生成 (Gemini Embedding API → pgvector)
  ↓
6. 重複検出・グループ化 (cosine distance)
```

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

### 開発環境セットアップ

初回 clone 後、commit 前 hook を install:

```bash
uvx pre-commit install
```

これにより `git commit` 時に gitleaks (secret 検出) / hadolint (Dockerfile lint) / Ruff / Biome が staged diff に対して自動実行される。CI 側でも同じ hook が再実行されるため、`--no-verify` で bypass しても PR で fail する。

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

コードから導出可能な情報（ディレクトリ構成、DB スキーマ、API 仕様）はドキュメント化せず、
コード自体を SSoT とする方針。`docs/` には「コードに書けないもの」のみを残す。

### 設計判断記録 (ADR)

- [`docs/adr/001_taskiq_over_arq.md`](docs/adr/001_taskiq_over_arq.md) — タスクキュー選定（taskiq 採用理由）
- [`docs/adr/002_auth_schema_separation.md`](docs/adr/002_auth_schema_separation.md) — PostgreSQL スキーマ分離（auth / public）
- [`docs/adr/003_bff_proxy_pattern.md`](docs/adr/003_bff_proxy_pattern.md) — BFF プロキシパターンによる認証

### その他

- [`docs/prompt_design.md`](docs/prompt_design.md) — AI 分析プロンプト設計ガイドライン
- [`domain.md`](domain.md) — ドメインモデルの棚卸し
