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

- テックニュースの自動収集（**45 source fetchers** — RSS / Atom / HTML listing / Sitemap、Hacker News API 含む）
- AI翻訳・要約・分析（**Gemini + DeepSeek** の multi-provider）
- 類似記事推薦（pgvector）
- ウォッチリスト（記事ブックマーク）
- **Trend Discovery** (`weekly_trends_snapshots`) と **週次 LLM ブリーフィング**
- **Pipeline Events 監査基盤** — 全ステージの成功/失敗/AI raw response を SQL で再構成可能
- キーワード・ニュースソース管理（管理者向け）
- カテゴリ別のニュースフィルタリング

## Tech Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Frontend | Next.js 16 (App Router) + React 19 + TypeScript | Tailwind CSS v4 + shadcn/ui + Biome |
| Backend | FastAPI + **Python 3.13** + SQLModel (SQLAlchemy 2.0 async) | Pydantic v2、非同期処理 |
| Auth | Better Auth (BFF Proxy) | Cookie ベースセッション + 内部 API ヘッダー認証 |
| Database | PostgreSQL 18 + pgvector | Alembic マイグレーション、**三重ロール** (vector_app / vector_auth / vector_collect) |
| AI | **Gemini + DeepSeek** (multi-provider) | Pure DI で [backend/app/queue/composition.py](backend/app/queue/composition.py) に hardcode。Gemini=curation/embedding、DeepSeek=assessment/briefing |
| Embedding | `gemini-embedding-001` (768-dim halfvec) | pgvector |
| Task Queue | taskiq + Redis (**7 broker 分離**) | metadata / content / analysis / embedding / trend_discovery / briefing / maintenance |
| CI/CD | GitHub Actions | lint + test + type check + 4 系統 security gate |
| Infrastructure | Docker Compose (dev) | **13 services** (常駐 9 + db-init 4)、internal network 中心 |
| Deployment | Fly.io (nrt region, 5 app) | core [fly.core.toml](backend/fly.core.toml) / collect [fly.collect.toml](backend/fly.collect.toml) / frontend [fly.toml](frontend/fly.toml) / redis [fly.toml](infra/redis/fly.toml) / redis-rl [fly.toml](infra/redis-rl/fly.toml) |

## Prerequisites

- Docker & Docker Compose
- **Gemini API Key** ([Google AI Studio](https://aistudio.google.com/) で取得) — curation (Stage 3) と embedding (Stage 5) に使用
- **DeepSeek API Key** (DeepSeek 公式) — assessment (Stage 4) と週次ブリーフィングに使用

## Getting Started

```bash
# 1. Clone & setup
cp .env.example .env
# .env に以下を設定:
#   - GEMINI_API_KEY / DEEPSEEK_API_KEY
#   - POSTGRES_PASSWORD / POSTGRES_AUTH_PASSWORD / POSTGRES_APP_PASSWORD / POSTGRES_COLLECT_PASSWORD (それぞれ openssl rand -hex 32)
#   - BETTER_AUTH_SECRET / BFF_JWT_SIGNING_SECRET / REVALIDATE_BEARER_SECRET (それぞれ openssl rand -hex 32、後者 2 つは別の値)
#   ※ secrets が未設定・弱い値・BFF と revalidate が同値だと backend / frontend は起動拒否

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
        ├── BFF Proxy (/api/proxy/*) → Backend (JWT header auth)
        ├── Rate limit (proxy.ts) → redis-rl (separate Redis instance)
        │
        └─► FastAPI Backend (Docker internal only)
              ├── Header Auth (HS256 JWT, BFF_JWT_SIGNING_SECRET)
              ├── collection/  — News Fetcher (45 sources: RSS/Atom/HTML/Sitemap + Hacker News API)
              ├── analysis/    — AI 分析 (Gemini curator / DeepSeek assessor / Gemini embedder)
              ├── insights/    — trend_discovery (weekly_trends) / briefing (weekly LLM brief)
              ├── audit/       — pipeline_events 監査 (Discriminated Union payload)
              ├── queue/       — taskiq broker / scheduler / composition (Pure DI)
              └── PostgreSQL 18 + pgvector (三重ロール: vector_app / vector_auth / vector_collect)

Redis (taskiq broker) ◄── worker-fetch / worker-analysis (analysis+embedding+maintenance) / worker-insights
                       ◄── scheduler (metadata / trend_discovery / briefing / maintenance cron)

Redis (rate-limit, ephemeral) ◄── proxy.ts IP sliding window log (rl:ip:*)
                                   ※ Better Auth ログイン limiter は DB (auth.rateLimit) 側
```

### Docker Compose サービス一覧 (常駐 9 + db-init 4 = 13 services)

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
[Stage 3] curation         — Gemini curator (gemini-2.5-flash-lite、タイトル正規化 / 構造抽出)
  ↓
[Stage 4] assessment       — DeepSeek assessor (deepseek-v4-flash、signal/noise + Category + Topic)
  ↓
[Stage 5] embedding        — Gemini Embedding (gemini-embedding-001, 768-dim halfvec)

並行 (broker_maintenance): back-fill 3 系統 (curation / assessment / embedding、kill switch あり) + pipeline_events retention purge
並行 (insights): Trend Discovery cron (weekly_trends snapshot) / 週次 briefing (DeepSeek)

各 stage は pipeline_events に監査記録 (Discriminated Union payload)。
業務処理と監査書込はアトミック (成功/skip パスは同 tx 内、失敗は別 tx で永続化)。
stage モジュールは [backend/app/audit/stages/](backend/app/audit/stages/) を SSoT とする。
詳細は [docs/observability/pipeline-events-design.md](docs/observability/pipeline-events-design.md)。
```

## Environment Variables

全変数の網羅は [.env.example](.env.example) が SSoT (現状 26 種)。ここでは README で補足すべき**非自明な制約**のみ挙げる。

### 起動時に検証される secrets ([backend/app/config.py](backend/app/config.py))

| 変数 | 制約 |
|------|------|
| `BFF_JWT_SIGNING_SECRET` | BFF→Backend の HS256 JWT 署名鍵。**32 文字未満 / 既知の弱い値で起動拒否** |
| `REVALIDATE_BEARER_SECRET` | Backend→Frontend revalidate 通知の Bearer。同じく強度検証。**`BFF_JWT_SIGNING_SECRET` と同値だと起動拒否** (片方漏洩で両境界が陥落するため) |
| `BETTER_AUTH_SECRET` | Better Auth セッション署名。`openssl rand -hex 32` |
| `DATABASE_URL` | 公開済 dev placeholder / 弱秘密パターンを起動拒否。`ENV=production` では `sslmode=require` 以上を強制 (平文不可) |
| `INTERNAL_FRONTEND_BASE_URL` | host allowlist (`localhost` / `127.0.0.1` / `frontend` / `*.flycast`)。`ENV=production` では `*.flycast` のみ (revalidate 通知先の SSRF 遮断) |

> dev fallback は撤去済み。weak default / placeholder は Settings 検証で起動失敗する。

### Database — 三重ロール

table owner / migration runner の `vector` を頂点に、用途別の最小権限ロールを分離する:

| ロール | 担当 | パスワード変数 |
|--------|------|----------------|
| `vector_app` | backend core (api / worker-analysis / worker-insights / scheduler) の `public.*` DML | `POSTGRES_APP_PASSWORD` |
| `vector_auth` | frontend Better Auth の `auth.*` DML | `POSTGRES_AUTH_PASSWORD` |
| `vector_collect` | collect worker 専用、acquisition + completion が触る 4 table のみの最小権限 | `POSTGRES_COLLECT_PASSWORD` |

本番 (Neon) では `DATABASE_URL` / `MIGRATION_DATABASE_URL` / `AUTH_DATABASE_URL` をロール別に分け、いずれも SSL verify-full に格上げする。

### AI provider

`GEMINI_API_KEY` / `DEEPSEEK_API_KEY` のみ使用 (Gemini=curation/embedding、DeepSeek=assessment/briefing)。provider・model 切替は [backend/app/queue/composition.py](backend/app/queue/composition.py) に hardcode する Pure DI 設計で、env による switch は持たない。`OPENAI_API_KEY` は `.env.example` に残るが現状 AI 分析には未使用 (RSS source "OpenAI News" の取得のみ)。

### Redis

`REDIS_URL` (taskiq broker / 本体) と `REDIS_URL_RL` (proxy.ts の IP rate-limit `rl:ip:*` 専用) を物理分離し、rate-limit 側の OOM が broker を道連れにしない設計。`REDIS_URL_RL` 未設定時は `REDIS_URL` に fallback。Better Auth ログイン limiter は DB-backed (`auth.rateLimit`, ADR-007) のため Redis 非依存。

### その他

- Backfill kill switch: `BACKFILL_CURATIONS_ENABLED` / `BACKFILL_ASSESSMENTS_ENABLED` / `BACKFILL_EMBEDDINGS_ENABLED`
- E2E seed (`E2E_SEED_*`) は CI 専用。seed script は `ENV=production` で fail-fast するため本番 env には置かない

## Development

### 開発環境セットアップ

初回 clone 後、commit 前 hook を install:

```bash
uvx pre-commit install
```

これにより `git commit` 時に gitleaks (secret 検出) / hadolint (Dockerfile lint) / Ruff / Biome が staged diff に対して自動実行される。CI 側でも同じ hook が再実行されるため、`--no-verify` で bypass しても PR で fail する。

### CI security gate

PR / push に対し GitHub Actions 上で **blocking gate** が自動実行される。詳細設定は各 workflow が SSoT:

- [security-pr.yml](.github/workflows/security-pr.yml) — osv-scanner (lockfile SCA) + npm audit (`--audit-level=high`) + Semgrep CE (`p/owasp-top-ten` + `p/security-audit`)
- [security-nightly.yml](.github/workflows/security-nightly.yml) — Trivy fs / config scan (HIGH+CRITICAL)
- [schemathesis-nightly.yml](.github/workflows/schemathesis-nightly.yml) — FastAPI `/openapi.json` と実装の適合性 fuzz (Schemathesis, GET 中心)
- [ci.yml](.github/workflows/ci.yml) — Ruff / Biome / tsc + unit / integration test + Playwright E2E smoke

新規 finding は CI fail として PR を block する。検出結果は Actions Artifacts に退避する (private repo + GitHub Free のため GHAS Code scanning が無効。SARIF を Security tab に上げず Artifacts に保存する設計)。

ローカル再現:

```bash
docker run --rm -v "${PWD}:/src" -w /src ghcr.io/google/osv-scanner:v2 -r ./   # OSV
cd frontend && npm audit --omit=dev --audit-level=high                          # npm audit
pip install semgrep && semgrep --config=p/owasp-top-ten --config=p/security-audit .  # Semgrep
```

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

### 型生成パイプライン

Backend の Pydantic schemas が SSoT (Single Source of Truth)。変更後は型を再生成する:

```bash
# Backend 起動中に実行
cd frontend && npm run generate-types
```