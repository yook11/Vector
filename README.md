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

- テックニュースの自動収集（**44+ source fetchers** — RSS / Atom / HTML listing / Sitemap、Hacker News API 含む）
- AI翻訳・要約・インパクト分析（**Gemini + DeepSeek + OpenAI** の multi-provider）
- 類似記事推薦（pgvector）
- 重複記事の自動検出・グループ化
- ウォッチリスト（記事ブックマーク）
- **Trend Discovery** (`weekly_trends_snapshots`) と **週次 LLM ブリーフィング**
- **Pipeline Events 監査基盤** — 全ステージの成功/失敗/AI raw response を SQL で再構成可能
- キーワード・ニュースソース管理（管理者向け）
- カテゴリ別のニュースフィルタリング

## Tech Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Frontend | Next.js 16 (App Router) + TypeScript | Tailwind CSS + shadcn/ui + Biome |
| Backend | FastAPI + **Python 3.13** + SQLModel (SQLAlchemy 2.0 async) | Pydantic v2、非同期処理 |
| Auth | Better Auth (BFF Proxy) | Cookie ベースセッション + 内部 API ヘッダー認証 |
| Database | PostgreSQL 18 + pgvector | Alembic マイグレーション、**二重ロール** (vector_app / vector_auth) |
| AI | **Gemini + DeepSeek + OpenAI** (multi-provider) | Pure DI で `backend/app/brokers.py` に hardcode |
| Embedding | `gemini-embedding-001` (768-dim halfvec) | pgvector |
| Task Queue | taskiq + Redis (**6 broker 分離**) | metadata / content / analysis / embedding / trend_discovery / briefing |
| CI/CD | GitHub Actions | lint + test + type check + 4 系統 security gate |
| Infrastructure | Docker Compose (dev) | **9 services**、internal network 中心 |
| Deployment | Fly.io (`your-vector-backend-app`, nrt region) | 本番設定は [backend/fly.toml](backend/fly.toml) |

## Prerequisites

- Docker & Docker Compose
- **Gemini API Key** ([Google AI Studio](https://aistudio.google.com/) で取得) — Stage 1 と Embedding に使用
- **DeepSeek API Key** (DeepSeek 公式) — Stage 4 (assessor) に使用
- **OpenAI API Key** (任意) — fallback / 比較用

## Getting Started

```bash
# 1. Clone & setup
cp .env.example .env
# .env に以下を設定:
#   - GEMINI_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY
#   - POSTGRES_PASSWORD / POSTGRES_AUTH_PASSWORD / POSTGRES_APP_PASSWORD (それぞれ openssl rand -hex 32)
#   - BETTER_AUTH_SECRET / INTERNAL_API_SECRET (それぞれ openssl rand -hex 32)
#   ※ secrets が未設定または弱い値だと backend / frontend は起動拒否

# 2. Start all services
docker compose up -d --build

# 3. Access
# Frontend: http://localhost:3000 (唯一の host-exposed エントリポイント)
```

> **Note (初回 / fresh dev volume)**: `docker compose up -d --build` 一発で
> fresh volume が立ち上がる。内部的には `db-init-*` 4 service が `db` healthy
> 後に `service_completed_successfully` で直列実行される:
>
> 1. `db-init-schema` — `auth` schema 作成 + `vector_auth` に一時 CREATE 権限付与
> 2. `db-init-better-auth` — Better Auth CLI migrate (`auth.user/session/account/verification` 作成)
> 3. `db-init-revoke-create` — 一時 CREATE 権限を REVOKE
> 4. `db-init-alembic` — `alembic upgrade head` (`c7` で `auth.user` への cross-schema FK を張る)
>
> 全 step は冪等 (`CREATE SCHEMA IF NOT EXISTS` / Better Auth CLI 自体冪等 /
> `alembic upgrade head` は head 到達済なら no-op)。既存 volume では数秒以内に
> 通過する。docker compose を経由せず手動で順序を踏みたい場合は
> [scripts/init-fresh-dev.sh](scripts/init-fresh-dev.sh) が同じ手順を 1 コマンドで流す。
>
> **Apple Silicon / aarch64 ホストでの注意**: `@better-auth/cli@1.4.22` は
> `better-sqlite3` を dependencies に持ち、後者は linux-arm64 の Node prebuild
> を提供しない ([WiseLibs/better-sqlite3 releases](https://github.com/WiseLibs/better-sqlite3/releases))。
> `db-init-better-auth` service は `NPM_CONFIG_IGNORE_SCRIPTS=true` で
> post-install を抑止して回避している (CLI は pg/kysely しか load しないため
> 機能影響なし)。Better Auth 1.5 stable で `better-sqlite3` が dependencies から
> 外れ次第 ([PR #7771](https://github.com/better-auth/better-auth/pull/7771))、この workaround は撤去する。

> **Note**: backend / db / redis / redis-rl / 3 worker / scheduler は全て Docker 内部
> ネットワーク (`internal: true`) のみで動作し host port を持ちません。
> フロントエンド (BFF) がプロキシとして機能するため、ブラウザからは
> `localhost:3000` のみにアクセスします。db に直接 psql したい場合は
> `docker compose exec db psql -U vector -d vector` を使ってください。

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
        ├── Rate limit (proxy.ts) → redis-rl (separate Redis instance)
        │
        └─► FastAPI Backend (Docker internal only)
              ├── Header Auth (X-User-ID / X-Internal-Secret)
              ├── collection/   — News Fetcher (44+ sources: RSS/Atom/HTML/Sitemap)
              ├── analysis/     — AI 分析 (Gemini extractor / DeepSeek assessor / Gemini embedder)
              ├── insights/     — trend_discovery (weekly_trends) / briefing (weekly LLM brief)
              ├── observability/— pipeline_events 監査 (Discriminated Union payload)
              ├── maintenance/  — back-fill backlog / budget / policy
              └── PostgreSQL 18 + pgvector (二重ロール: vector_app / vector_auth)

Redis (taskiq broker) ◄── worker-fetch / worker-analysis (analysis+embedding) / worker-insights
                       ◄── scheduler (metadata / trend_discovery / briefing cron)

Redis (rate-limit, ephemeral) ◄── proxy.ts IP sliding window log (rl:ip:*)
                                   ※ Better Auth ログイン limiter は DB (auth.rateLimit) 側
```

### Docker Compose サービス一覧 (9 services)

| Service | Description |
|---------|------------|
| `frontend` | Next.js 16 BFF — 唯一の public エントリーポイント |
| `backend` | FastAPI — internal network 限定 |
| `db` | PostgreSQL 18 + pgvector (`internal: true`、host port 非公開) |
| `redis` | taskiq broker (本体)、`maxmemory 256mb` + `allkeys-lru` |
| `redis-rl` | proxy.ts の IP rate-limit (`rl:ip:*`) 専用 / `volatile-ttl` (本体 redis と OOM 道連れを防ぐ物理分離)。Better Auth ログイン limiter は DB-backed (`auth.rateLimit`, ADR-007) |
| `worker-fetch` | metadata + content fetch worker (supervisord Pattern B) |
| `worker-analysis` | AI 分類 / 分析 + Embedding 生成 worker (supervisord Pattern B) |
| `worker-insights` | Trend Discovery + briefing worker (supervisord Pattern B) |
| `scheduler` | metadata / trend_discovery / briefing cron scheduler (supervisord Pattern B) |

> **supervisord Pattern B**: `autorestart=unexpected` + `startretries=3` + FATAL listener。
> 一過性故障は吸収しつつ、永続バグは 3 retries で FATAL → fail_fast eventlistener が
> supervisord shutdown → container exit → docker / Fly.io が auto-restart loop で
> visible 化する設計。

## Domain Concepts

| 概念 | 説明 | モデル |
|------|------|--------|
| ニュース記事 | 収集・翻訳されたテックニュース | `NewsArticle` |
| AI分析結果 | AIによる翻訳・要約・インパクト分析 | `ArticleAnalysis` |
| キーワード | 記事のタギングに使用される検索キーワード | `Keyword` |
| カテゴリ | キーワードと記事を分類する統合カテゴリ | `Category` |
| ニュースソース | ニュースの収集元メディア・サイト | `NewsSource` |
| ウォッチリスト | ユーザーの記事ブックマーク | `WatchlistEntry` |
| 週次トレンドスナップショット | 週次ダイジェスト用の集約結果 | `WeeklyTrendsSnapshot` |
| 週次ブリーフィング | 週次 LLM 生成サマリー (カテゴリ別) | `WeeklyBriefing` |
| パイプラインイベント | 全 stage 監査ログ (Discriminated Union payload) | `PipelineEvent` |
### ニュース処理パイプライン

```
taskiq scheduler (cron) → broker_metadata 投函
  ↓
[Stage 1] acquisition      — アクティブソースから記事メタデータを取得
                             (RSS/Atom/HTML listing/Sitemap/Hacker News API)
                             + 新規記事 row 作成
  ↓
[Stage 2] completion       — 全文取得 (trafilatura) で記事本体を充足
  ↓
[Stage 3] curation         — Gemini extractor (タイトル正規化 / 構造抽出)
  ↓
[Stage 4] assessment       — DeepSeek assessor (signal/noise + Category + Topic)
  ↓
[Stage 5] embedding        — Gemini Embedding (gemini-embedding-001, 768-dim halfvec)

並行: back-fill 3 系統 (curation / assessment / embedding、kill switch あり)
並行: Trend Discovery cron (weekly_trends snapshot)、briefing cron (週次 LLM ブリーフィング)

各 stage は pipeline_events に監査記録 (Discriminated Union payload)。
業務処理と監査書込はアトミック (成功/skip パスは同 tx 内、失敗は別 tx で永続化)。
stage モジュールは [backend/app/audit/stages/](backend/app/audit/stages/) を SSoT とする。
詳細は [docs/observability/pipeline-events-design.md](docs/observability/pipeline-events-design.md)。
```

## Environment Variables

`.env.example` を参照。主要な変数 (全 26 種):

### Deployment

| Variable | Default | Description |
|----------|---------|-------------|
| `ENV` | `development` | `production` で `/docs` `/redoc` `/openapi.json` を無効化 |

### Database (二重ロール)

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_USER` | `vector` | Table owner / migration runner |
| `POSTGRES_PASSWORD` | — | `vector` ロールのパスワード (必須、強い乱数) |
| `POSTGRES_DB` | `vector` | DB 名 |
| `POSTGRES_AUTH_PASSWORD` | — | `vector_auth` ロール (frontend Better Auth 専用) |
| `POSTGRES_APP_PASSWORD` | — | `vector_app` ロール (backend / worker / scheduler 専用) |

### AI

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Stage 1 (extraction) + Embedding 用 (必須) |
| `DEEPSEEK_API_KEY` | — | Stage 2 (classification) 用 (必須) |
| `OPENAI_API_KEY` | — | fallback / 比較用 (任意) |

> Provider 切替は `backend/app/brokers.py` に hardcode する Pure DI 設計。
> env による provider switch は持たない。

### News Fetcher

| Variable | Default | Description |
|----------|---------|-------------|
| `FETCH_INTERVAL_HOURS` | `12` | スケジューラー実行間隔 |
| `MAX_ARTICLES_PER_FETCH` | `50` | 1 回のフェッチ上限 |
| `MAX_ANALYSIS_PER_RUN` | `10` | 1 回の分析対象上限 |
| `CONTENT_MAX_LENGTH` | `8000` | 全文抽出の最大文字数 |

### Auth (Better Auth + BFF Proxy)

| Variable | Default | Description |
|----------|---------|-------------|
| `BETTER_AUTH_SECRET` | — | **未設定なら起動拒否**。`openssl rand -hex 32` で生成 |
| `BETTER_AUTH_URL` | `http://localhost:3000` | コールバック URL |
| `INTERNAL_API_SECRET` | — | **未設定または 32 文字未満なら起動拒否**。BFF→Backend 認証 |

> dev fallback は撤去済み (PR #405 / #406 / #407)。weak default や placeholder は
> backend の Settings 検証で起動失敗する。

### Task Queue

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://redis:6379/0` | taskiq broker (本体) |
| `REDIS_URL_RL` | `redis://redis-rl:6379/0` | proxy.ts の IP rate-limit (`rl:ip:*`) 専用 (未設定なら REDIS_URL に fallback)。Better Auth ログイン limiter は DB-backed のため Redis 不要 |
| `REDIS_PORT` | `6379` | host から起動するときの参考値 |
| `RATE_LIMIT_PER_MIN` | `60` | proxy.ts per-IP sliding window 上限 |

### Backfill kill switches

| Variable | Default | Description |
|----------|---------|-------------|
| `BACKFILL_EXTRACTIONS_ENABLED` | `true` | extraction back-fill の有効化 |
| `BACKFILL_CLASSIFICATIONS_ENABLED` | `true` | classification back-fill の有効化 |
| `BACKFILL_EMBEDDINGS_ENABLED` | `true` | embedding back-fill の有効化 |

### App URLs

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTEND_URL` | `http://localhost:3000` | CORS 許可オリジン |
| `INTERNAL_API_URL` | `http://backend:8000/api/v1` | SSR からの Backend 接続先 (Docker 内部) |
| `INTERNAL_FRONTEND_BASE_URL` | `http://frontend:3000` | backend → frontend internal (revalidate 通知など) |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000/api/v1` | Frontend public API base |

### E2E Test Seed (CI 専用、本番設定禁止)

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_SEED_USER_EMAIL` | `e2e@example.com` | seed_e2e_users.py が読む |
| `E2E_SEED_USER_PASSWORD` | `Password123!` | 同上 |
| `E2E_SEED_ADMIN_EMAIL` | `e2e-admin@example.com` | 同上 |
| `E2E_SEED_ADMIN_PASSWORD` | `Password123!` | 同上 |

> seed script は `ENV=production` で fail-fast。env 自体を本番に置かないのが第一の防護壁。

## Development

### 開発環境セットアップ

初回 clone 後、commit 前 hook を install:

```bash
uvx pre-commit install
```

これにより `git commit` 時に gitleaks (secret 検出) / hadolint (Dockerfile lint) / Ruff / Biome が staged diff に対して自動実行される。CI 側でも同じ hook が再実行されるため、`--no-verify` で bypass しても PR で fail する。

### CI security gate

PR / push に対し以下のセキュリティスキャンが GitHub Actions 上で **blocking gate** として自動実行される
([.github/workflows/security-pr.yml](.github/workflows/security-pr.yml)):

- **osv-scanner**: `backend/uv.lock` + `frontend/package-lock.json` を OSV.dev 脆弱性 DB と照合 (`fail-on-vuln: true`)
- **npm audit**: frontend の production 依存を npm Advisory DB と照合 (`--audit-level=high`)
- **Semgrep CE**: backend / frontend のソースを `p/owasp-top-ten` + `p/security-audit` ruleset で静的解析

新規 finding は CI fail として PR を block する。検出結果は GitHub Actions の Artifacts
(`osv-results.sarif` / `semgrep-sarif` / `npm-audit-json`) として triage 用に保存される。
回避策が複雑な finding は `# nosemgrep` (rule 単位) または dep update / 例外 PR で対応する。

加えて nightly に [.github/workflows/security-nightly.yml](.github/workflows/security-nightly.yml) の Trivy fs/config scan が
HIGH+CRITICAL を `exit-code: '1'` で blocking 実行する。

> **Note**: Vector は private repo + GitHub Free のため Code scanning (GHAS) が
> 無効。SARIF を Security tab に upload せず Actions Artifacts に保存する設計。
> GHAS 契約 or repo public 化で `upload-sarif: true` に戻せば Security tab で
> 一覧確認可能になる。

ローカル再現:

```bash
# OSV-Scanner (Docker 経由)
docker run --rm -v "${PWD}:/src" -w /src ghcr.io/google/osv-scanner:v2 -r ./

# npm audit
cd frontend && npm audit --omit=dev --audit-level=high

# Semgrep CE
pip install semgrep
semgrep --config=p/owasp-top-ten --config=p/security-audit .
```

### Schemathesis (API 仕様適合性 fuzz)

[.github/workflows/schemathesis-nightly.yml](.github/workflows/schemathesis-nightly.yml) が nightly (JST 03:37) に
FastAPI `/openapi.json` と実装の適合性を Schemathesis (Hypothesis ベース
property-based fuzz) で検査する。3 check (`not_a_server_error` /
`status_code_conformance` / `response_schema_conformance`) を初回は GET only +
25 examples で **warn-only** 実行し、findings は Actions Artifacts
(`schemathesis-results`) に JUnit XML + VCR cassette で 14 日保存する。
triage 完了後に blocking 化、その後 mutation method (POST/PUT/DELETE) や
stateful 拡張を別 PR で順次着手する予定。

### テスト・lint 実行

```bash
# Backend
docker compose exec backend ruff check app/
docker compose exec backend ruff format --check app/
docker compose exec backend python -m pytest tests/ -x -q

# Frontend
docker compose exec frontend npx biome check src/
docker compose exec frontend npx tsc --noEmit
docker compose exec frontend npm test
```

#### DB を要するテスト (integration)

`-m integration` のテストは host から専用 `db-test` (`127.0.0.1` の random port, project 名 `vector-test-<worktree>`) を立てて回す。Makefile が `DATABASE_URL` / `MIGRATION_DATABASE_URL` / role password を OS env で注入するため **`.env` は不要**。worktree 直下からも `.env` symlink なしで動き、project 名・ポートが worktree ごとに分離されるため複数 worktree で並列実行できる。

```bash
# 全 integration テスト
make test-integration

# 個別ファイル / マーカー絞り込み
make test-integration PYTEST_ARGS='tests/path/to_test.py -q'
make test-integration PYTEST_ARGS='-k "search and quota"'
```

`uv run pytest` を直接叩くと `.env` 不在時に conftest が dummy DB (`unreachable.invalid`) にフォールバックするため、DB 接続が要るテストは必ず `make test-integration` 経由で回すこと。終了時は `trap` で `down -v --remove-orphans` するため tmpfs ごと毎回 fresh。

### DB 操作

```bash
# マイグレーション実行
docker compose exec backend alembic upgrade head

# ロールバック
docker compose exec backend alembic downgrade -1

# DB 接続
docker compose exec db psql -U vector -d vector
```

### 型生成パイプライン

Backend の Pydantic schemas が SSoT (Single Source of Truth)。変更後は型を再生成する:

```bash
# Backend 起動中に実行
cd frontend && npm run generate-types
```

### Worktree 運用

`.env` は `.gitignore` 対象のため `git worktree add` には追従しない。`docker compose up` や `docker compose exec backend ...` を worktree 側から叩く場合に必要なので、専用ヘルパで main worktree の `.env` を symlink する。

```bash
# 新規 worktree 作成 (git worktree add の引数をそのまま渡す)
scripts/worktree-add.sh ../Vector-foo feature/foo

# 既存 worktree のうち .env が欠落しているものを一括補修
scripts/worktree-fix-env.sh --dry-run   # まず差分確認
scripts/worktree-fix-env.sh             # 実行
```

- 既存の `.env` (通常ファイル / symlink どちらも) は **絶対に上書きしない** — `WARN` / `SKIP` で報告するだけ
- symlink 元は既定で main worktree の `.env`。`VECTOR_ENV_SOURCE=/path/to/.env` で上書き可能
- DB を要するテスト (`-m integration`) は `.env` 不要なので `make test-integration` 経由が第一選択（上記「DB を要するテスト」参照）

## Documentation

コードから導出可能な情報（ディレクトリ構成、DB スキーマ、API 仕様）はドキュメント化せず、
コード自体を SSoT とする方針。`docs/` には「コードに書けないもの」のみを残す。

### 設計判断記録 (ADR)

- [`docs/adr/001_taskiq_over_arq.md`](docs/adr/001_taskiq_over_arq.md) — タスクキュー選定（taskiq 採用理由）
- [`docs/adr/002_auth_schema_separation.md`](docs/adr/002_auth_schema_separation.md) — PostgreSQL スキーマ分離（auth / public）
- [`docs/adr/003_bff_proxy_pattern.md`](docs/adr/003_bff_proxy_pattern.md) — BFF プロキシパターンによる認証
- [`docs/adr/004_unit_of_work_service_convention.md`](docs/adr/004_unit_of_work_service_convention.md) — Unit of Work / Service 規約
- [`docs/adr/005_rsc_test_strategy.md`](docs/adr/005_rsc_test_strategy.md) — RSC ユニットテスト戦略 (Vitest projects 分離 + page-models)
- [`docs/adr/006_better_auth_rate_limit_strategy.md`](docs/adr/006_better_auth_rate_limit_strategy.md) — Better Auth + proxy.ts 二段 rate limit
- [`docs/adr/sqlmodel-to-declarative-migration.md`](docs/adr/sqlmodel-to-declarative-migration.md) — SQLModel → SQLAlchemy declarative 移行プラン
- [`docs/adr/value_objects_sqlalchemy_migration.md`](docs/adr/value_objects_sqlalchemy_migration.md) — Value Objects の SQLAlchemy 移行プラン

### その他

- [`docs/observability/pipeline-events-design.md`](docs/observability/pipeline-events-design.md) — pipeline_events 監査基盤 ADR
- [`docs/prompt_design.md`](docs/prompt_design.md) — AI 分析プロンプト設計ガイドライン
- [`specs/domain.md`](specs/domain.md) — ドメインモデルの棚卸し
