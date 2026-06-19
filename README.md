# Vector

> 海外テックニュース収集・AI翻訳・投資分析ダッシュボード

次世代コンピューティング、マテリアル・インフォマティクスなど、
日本では情報が少ない先端分野の海外ニュースを自動収集し、
AIで翻訳・要約・インパクト分析を行う投資ダッシュボード。

## 画面プレビュー

Vector は、海外の先端テックニュースを自動収集し、AI で日本語に翻訳・要約したうえで、投資判断に必要な要点・背景・トレンドを確認できるダッシュボードです。

![カテゴリ別に収集された海外テックニュースを、日本語の要点付きで一覧できるニュースダッシュボード](docs/assets/readme/01-dashboard.png)

## 主な画面

| ニュース詳細 | トレンド分析 |
|---|---|
| ![AI が翻訳・要約した記事詳細画面。要点と背景文脈を確認できる。](docs/assets/readme/02-article-detail.png) | ![カテゴリ別の注目ワードと急上昇ワードを表示するトレンド画面。](docs/assets/readme/03-trends.png) |
| AI が記事を翻訳・要約し、要点と背景文脈を整理する。 | カテゴリごとに言及数と急上昇ワードを集計し、直近の注目テーマを把握する。 |

| 週次ブリーフィング | ブリーフィング詳細 |
|---|---|
| ![カテゴリごとの週次ブリーフィング一覧。AI が週単位の論点を要約する。](docs/assets/readme/04-briefing.png) | ![週次ブリーフィングの詳細画面。複数記事から生成された市場・技術動向の要約を読める。](docs/assets/readme/05-briefing-detail.png) |
| カテゴリごとに、1週間の重要なニュースと論点をまとめる。 | 複数記事をもとに、週次の市場・技術動向を読み物として整理する。 |

### 解決する課題

- 海外テックニュースは英語記事が多く、日本語話者の投資家が継続的に追うには負荷が高い
- 日々の記事は断片的で、AI・半導体・宇宙などの分野ごとに「今週何が起きたのか」を把握しづらい
- 投資判断の前段で必要な要点・背景・流れを拾うために、複数の記事を読み比べる時間がかかる

### 主要機能

- テックニュースの自動収集
- AI による日本語翻訳・要約・背景整理
- カテゴリ別の記事一覧とフィルタリング
- 関連記事推薦
- 週次 LLM ブリーフィング
- 注目ワード / 急上昇ワードの集計

### 実装上の特徴

- 45 source fetchers による RSS / Atom / HTML / Sitemap / Hacker News API 収集
- Gemini + DeepSeek を用途別に使い分ける multi-provider 構成
- pgvector の HALFVEC(768) による関連記事検索
- taskiq + Redis Streams による 7 queue の非同期処理
- Pipeline Events による収集・分析パイプラインの監査ログ化
- Next.js BFF、FastAPI internal API、PostgreSQL ロール分離による境界設計
- Fly.io + Neon PostgreSQL による本番デプロイ構成

## 学習と設計への取り組み

Vector は、技術書などで学んだ内容を、実際のプロダクト設計・実装・運用設計に落とし込むことを意識して開発しています。

特に、実装前に何を達成するのか？・満たさないといけない条件・完了条件を整理する仕様書駆動開発、specsによる設計判断の記録、ドメインモデルの整理
CI / security gate / 監査ログなど、長期的に保守できる開発プロセスを意識しています。

AI は単なるコード生成ではなく、仕様整理、設計レビュー、テスト観点の洗い出し、ドキュメント改善を支援する開発パートナーとして活用しています。[AGENTS.md](AGENTS.md) や project-specific skills、sub-agent を整備し、AI に任せる範囲と人間が判断する範囲を意識しながら開発しています。

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 16, React 19, TypeScript, Tailwind CSS v4, shadcn/ui |
| Backend | FastAPI, Python 3.13, SQLAlchemy 2.0 async, Pydantic v2 |
| Auth | Better Auth, BFF Proxy, internal JWT |
| Database | PostgreSQL 18, pgvector, Alembic |
| AI | Gemini, DeepSeek, gemini-embedding-001 |
| Queue | taskiq, Redis Streams |
| Infrastructure | Docker Compose (dev), Fly.io, Neon PostgreSQL |
| CI/CD | GitHub Actions, Ruff, Biome, pytest, Vitest, Playwright |

## Getting Started

ローカルで動かすには Docker / Docker Compose と、Gemini・DeepSeek の API key が必要です。

```bash
cp .env.example .env
# .env を編集して API key と secret を設定
docker compose up -d --build
```

起動後、ブラウザで `http://localhost:3000` を開きます。

環境変数の一覧と制約は [.env.example](.env.example) を参照してください。secret には `openssl rand -hex 32` などで生成した十分に長い値を使います。`BFF_JWT_SIGNING_SECRET` と `REVALIDATE_BEARER_SECRET` は別の値にしてください。

補足: 初回起動時の DB schema 作成、Better Auth migration、Alembic migration は `docker compose up -d --build` 内で自動実行されます。

ローカル環境では `frontend` だけが `localhost:3000` に公開され、backend / db / redis / worker は Docker 内部ネットワークで動作します。

## Architecture

Vector は Next.js BFF を唯一の公開入口にし、FastAPI backend と worker 群を内部ネットワーク側に閉じる構成です。本番環境では Fly.io の 5 app と Neon PostgreSQL で動作します。

```text
Browser
  │
  ▼
Fly: vector-frontend
Next.js BFF / Better Auth / proxy
  │
  ├── Redis RL: vector-redis-rl
  │     frontend proxy の rate limit 判定に使用
  │
  ▼
Fly: vector-core
FastAPI API / AI analysis workers / scheduler
  │
  ├── Neon PostgreSQL + pgvector
  └── Redis Broker: vector-redis
        ▲
        │
Fly: vector-collect
News fetch / HTML extraction workers
```

### Production Apps

| App | Role |
|-----|------|
| `vector-frontend` | Public entry point。Next.js BFF、認証、proxy、rate limit |
| `vector-core` | FastAPI API、AI 分析、embedding、briefing、scheduler |
| `vector-collect` | 外部ニュースサイトの取得・本文抽出専用 worker |
| `vector-redis` | taskiq / Redis Streams broker |
| `vector-redis-rl` | frontend rate limit 専用 Redis |
| Neon PostgreSQL | application DB + pgvector |

設計判断の背景は [docs/architecture.md](docs/architecture.md) に簡単にまとめています。詳細な決定記録は [docs/adr/README.md](docs/adr/README.md) を参照してください。


## ニュース処理パイプライン

Vector は、ニュース収集から AI 分析、関連記事検索、週次ブリーフィングまでを taskiq + Redis Streams の非同期パイプラインで処理します。

```text
[Stage 1] acquisition
  RSS / Atom / HTML listing / Sitemap / Hacker News API から記事メタデータを取得し、
  新規記事候補を作成する。

[Stage 2] completion
  trafilatura で本文を取得し、分析に必要な記事本文を補完する。

[Stage 3] curation
  Gemini で日本語タイトル・要約・構造情報を生成する。

[Stage 4] assessment
  DeepSeek で signal/noise、カテゴリ、投資判断向けの文脈を整理する。

[Stage 5] embedding
  gemini-embedding-001 で 768 次元 embedding を生成し、
  pgvector の halfvec に保存して関連記事検索に使う。
```

並行して、Trend Discovery と週次 LLM ブリーフィングを cron で生成します。AI provider の一時失敗や quota で止まった記事は、cron 駆動の back-fill queue が curation / assessment / embedding の未完了分を再投入します。

各 stage の成功・失敗・AI raw response は Pipeline Events として記録し、非同期処理で起きた問題を後から追跡できるようにしています。詳細は [docs/observability/pipeline-events-design.md](docs/observability/pipeline-events-design.md) を参照してください。

## Environment

環境変数の一覧と制約は [.env.example](.env.example) を参照してください。

Vector は起動時に secret の強度、内部 URL の allowlist、本番 DB の SSL 設定などを検証します。また、本番では app / auth / collect の DB ロールを分離し、Redis も broker 用と rate limit 用に分けています。

環境変数と実行時境界の詳細は [docs/architecture.md](docs/architecture.md) にまとめています。

## Development

開発時は pre-commit と GitHub Actions で lint / format / type check / test / security scan を実行します。

```bash
uvx pre-commit install
```

主な検証:

- Backend: Ruff, pytest, integration tests
- Frontend: Biome, TypeScript, Vitest, Playwright
- Security: gitleaks, hadolint, OSV, npm audit, Semgrep, Trivy
- API contract: Schemathesis, OpenAPI 生成型

詳細なコマンドと CI 構成は [docs/development.md](docs/development.md) を参照してください。
