# Architecture

Vector は、海外テックニュースの収集、AI 翻訳・分析、週次ブリーフィング生成を非同期パイプラインで処理するアプリケーションです。

本番環境では Fly.io の 5 app と Neon PostgreSQL で動作します。

```text
Browser
  │
  ▼
vector-frontend
Next.js BFF / Better Auth / proxy
  │
  ├── vector-redis-rl
  │     frontend proxy の rate limit 判定に使用
  │
  ▼
vector-core
FastAPI API / AI analysis workers / scheduler
  │
  ├── Neon PostgreSQL + pgvector
  └── vector-redis
        ▲
        │
vector-collect
News fetch / HTML extraction workers
```

## 構成判断

### 公開入口を Next.js BFF に集約する

ブラウザから直接到達できる入口は `vector-frontend` に集約する。FastAPI backend は内部 API として扱い、BFF が Better Auth のセッションを検証して backend 向けの短期 JWT を発行する。

これにより、ブラウザ向けの認証境界と backend の内部 API 境界を分ける。

### backend と collect worker を分ける

`vector-core` は API、AI 分析、embedding、briefing、scheduler を担う。`vector-collect` は外部ニュースサイトへの fetch と HTML 抽出だけを担う。

外部サイトの HTML を扱う worker を AI key を持つ core app から分離し、侵害時の影響範囲を狭める。

### Redis を用途別に分ける

`vector-redis` は taskiq / Redis Streams の broker として使う。`vector-redis-rl` は frontend proxy の rate limit 専用に使う。

Rate limit 用の揮発的な key と、worker queue の message を同じ Redis に載せないことで、片方の障害がもう片方を巻き込まないようにする。

`vector-redis-rl` はリクエストの「後段」ではなく、`vector-frontend` が rate limit 判定のために問い合わせる横の依存である。

### Neon PostgreSQL を正本 DB にする

アプリケーション DB は Neon PostgreSQL を使い、pgvector による関連記事検索も同じ DB に載せる。

本番では app / auth / collect のロールを分け、アプリ本体、Better Auth、収集 worker の権限境界を分離する。

### 非同期処理を queue に分ける

ニュース収集、本文抽出、AI 分析、embedding、trend discovery、briefing、maintenance は taskiq + Redis Streams で分けて処理する。

重い AI 処理や外部 fetch を HTTP request path から外し、失敗時は Pipeline Events に監査ログを残す。

## セキュリティと実行時境界

### Environment validation

環境変数の一覧は [`.env.example`](../.env.example) を正本にする。起動時の検証は [`backend/app/config.py`](../backend/app/config.py) に集約している。

- `BFF_JWT_SIGNING_SECRET` / `REVALIDATE_BEARER_SECRET` は強度を検証し、既知の弱い値や短すぎる値を拒否する
- `BFF_JWT_SIGNING_SECRET` と `REVALIDATE_BEARER_SECRET` が同値の場合は起動を拒否する
- `DATABASE_URL` は公開済み placeholder や弱い秘密パターンを拒否し、本番では SSL を必須にする
- `INTERNAL_FRONTEND_BASE_URL` は allowlist で検証し、本番では `*.flycast` のみに制限する

### Database roles

本番では Neon PostgreSQL に対して用途別のロールを分ける。

| Role | Purpose |
|------|---------|
| `vector_app` | backend core、worker、scheduler のアプリケーション DML |
| `vector_auth` | Better Auth の `auth.*` DML |
| `vector_collect` | collect worker が触る収集系 table の DML |

Migration は table owner / migration runner のロールで実行し、runtime のアプリケーションロールとは分ける。

### AI provider selection

AI provider の選択は env switch にしない。Gemini / DeepSeek の用途別配線は [`backend/app/queue/composition.py`](../backend/app/queue/composition.py) に集約し、worker 起動時に adapter を構築する。

- Gemini: curation / embedding
- DeepSeek: assessment / weekly briefing

この方針により、共有 env の設定ミスで stage ごとの provider が入れ替わる余地を減らす。

### Redis separation

`REDIS_URL` は taskiq / Redis Streams broker 用、`REDIS_URL_RL` は frontend proxy の rate limit 用に分ける。

Rate limit 用 Redis は揮発的な sliding-window key を扱う。一方、broker 用 Redis は worker queue の message を扱うため、key の性質と障害時の許容範囲が違う。2 つを分けることで、rate limit 側の key 増加や eviction が broker queue に影響しないようにする。

Better Auth のログイン rate limit は DB-backed で、Redis rate-limit とは別の境界に置く。

### Back-fill controls

curation / assessment / embedding の back-fill は kill switch を持つ。

- `BACKFILL_CURATIONS_ENABLED`
- `BACKFILL_ASSESSMENTS_ENABLED`
- `BACKFILL_EMBEDDINGS_ENABLED`

AI provider の一時失敗や quota による滞留を cron で救済しつつ、必要な場合は stage ごとに停止できるようにする。

## ローカル環境

ローカルでは Docker Compose で frontend / backend / db / redis / worker / scheduler をまとめて起動する。公開される host port は frontend の `localhost:3000` のみで、backend / db / redis / worker は Docker 内部ネットワークで動作する。

## 関連ドキュメント

- [ADR Index](adr/README.md)
- [Pipeline Events Design](observability/pipeline-events-design.md)
- [Pipeline Events Failure Attributes](observability/pipeline-events-failure-attributes.md)
