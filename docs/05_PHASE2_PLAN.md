# Phase 2 実装計画

## 前提

- 公開を前提としたプロダクション品質のアーキテクチャを選定する
- 技術的負債を最小化し、本番運用に耐える設計とする
- Phase 1 で認証・ユーザー別キーワード/ウォッチリスト機能は実装済み

## 実装順序

```
Step 0: OpenAPI 型自動生成パイプライン
  ↓
Step A: 記事の全文取得・分析
  ↓
Step B-0: タスクキュー PoC（arq vs taskiq）
  ↓
Step B: タスクキュー実装
  ↓
Step C: pgvector セマンティック検索
```

---

## Step 0: OpenAPI スキーマ & 型自動生成パイプライン

### 目的

バックエンドの Pydantic スキーマ変更がフロントエンドの TypeScript 型に
コマンド1つで反映される仕組みを構築する。

### パイプライン

```
FastAPI (Pydantic スキーマ, alias_generator=to_camel)
  → GET /openapi.json（FastAPI 自動生成）
  → openapi-typescript（CLI）
  → frontend/src/types/generated.ts
```

### タスク

#### 0-1. camelCase 出力の検証（最優先）

FastAPI の `/openapi.json` を実際に取得し、以下を確認する:

- スキーマのプロパティ名が camelCase（alias 側）で出力されているか
- `populate_by_name=True` の影響がないか
- Query パラメータの `alias="keywordId"` 等が正しく反映されているか

```bash
docker compose up -d backend db
curl http://localhost:8000/openapi.json | python -m json.tool > /tmp/openapi.json
# プロパティ名を目視確認
```

現在のスキーマ例 (`backend/app/schemas/news.py`):
```python
class NewsResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    title_original: str  # → OpenAPI で "titleOriginal" になるか確認
```

**もし snake_case で出力される場合:** FastAPI の `response_model` に
`by_alias=True` をグローバル設定するか、スキーマ側で `model_config` に
`serialize_by_alias=True` を追加する必要がある。

#### 0-2. openapi-typescript の導入

```bash
cd frontend
npm install -D openapi-typescript
```

#### 0-3. 型生成スクリプトの作成

`frontend/package.json` に追加:
```json
{
  "scripts": {
    "generate-types": "npx openapi-typescript http://localhost:8000/openapi.json -o src/types/generated.ts",
    "generate-types:file": "npx openapi-typescript ./openapi.json -o src/types/generated.ts"
  }
}
```

#### 0-4. 型ファイルの構成

```
frontend/src/types/
  ├── generated.ts   # openapi-typescript が自動生成（手動編集禁止）
  └── index.ts       # 手動の補助型（リエクスポート + 追加型）
```

`types/index.ts` に残すもの:
- `NewsQuery`（クエリパラメータ型 — OpenAPI にはあるが形式が異なる）
- ユーティリティ型（`Sentiment` リテラル型など）
- `generated.ts` からのリエクスポート

`types/index.ts` から削除するもの:
- `NewsResponse`, `PaginatedNewsResponse`, `AnalysisResponse` 等、
  バックエンドのレスポンススキーマと1:1対応する型 → `generated.ts` から使う

#### 0-5. 既存コンポーネントの移行

`generated.ts` の型名は OpenAPI のスキーマ名に基づく
（例: `components["schemas"]["NewsResponse"]`）。
既存コードで使いやすくするため、`types/index.ts` で型エイリアスを定義:

```typescript
import type { components } from "./generated";

export type NewsResponse = components["schemas"]["NewsResponse"];
export type PaginatedNewsResponse = components["schemas"]["PaginatedNewsResponse"];
// ...
```

これにより、既存のインポートを変更せずに移行できる。

#### 0-6. CI 統合

型生成は手動実行 + CI で検証する方式:

- 開発者: スキーマ変更後に `npm run generate-types` を実行
- CI (GitHub Actions): バックエンドを起動 → 型生成 → `tsc --noEmit` でコンパイル検証
- Docker Compose 起動時の自動生成は**採用しない**（タイミング問題のため）

### 完了条件

- [ ] `/openapi.json` の camelCase 出力を確認済み
- [ ] `npm run generate-types` で `types/generated.ts` が生成される
- [ ] 既存の全コンポーネントが生成型でコンパイルエラーなし
- [ ] `types/index.ts` がリエクスポート + 補助型のみに整理済み

---

## Step A: 記事の全文取得・分析

### 目的

RSS の要約（`description_original`）だけでなく記事本文を取得し、
AI 分析の精度を向上させる。

### データフロー（変更後）

```
[現在] RSS fetch → AI 分析（title + description のみ）

[Step A 後]
RSS fetch
  → 全文取得（newspaper4k）
  → AI 分析（title + description + content）
  → フロントエンド表示
```

### スケジューラーパイプライン（変更後）

現在の `scheduler.py` の `run_fetch_and_analyze()`:
```
Phase 1: アクティブキーワード取得
Phase 2: RSS フェッチ
Phase 3: 未分析記事の取得
Phase 4: AI 分析
```

変更後:
```
Phase 1: アクティブキーワード取得
Phase 2: RSS フェッチ
Phase 3: 本文未取得の記事を取得     ← 新規
Phase 4: 全文取得（newspaper4k）    ← 新規
Phase 5: 未分析記事の取得
Phase 6: AI 分析（全文対応）        ← 変更
```

### タスク

#### A-1. newspaper4k の導入

```
backend/requirements.txt に追加:
  newspaper4k>=0.9,<1
  lxml>=5.0,<6       # newspaper4k の依存
  lxml_html_clean     # newspaper4k の依存
```

#### A-2. NewsArticle モデルの拡張

`backend/app/models/news.py` に追加:
```python
content: str | None = Field(default=None)          # 記事本文
content_fetched_at: datetime | None = Field(        # 本文取得日時
    default=None, sa_type=DateTime(timezone=True)
)
```

Alembic マイグレーション:
```bash
alembic revision --autogenerate -m "add content fields to news_articles"
alembic upgrade head
```

#### A-3. 全文取得サービスの実装

`backend/app/services/content_extractor.py`:

```python
# 責務:
# - httpx で記事 URL にアクセスし HTML を取得
# - newspaper4k で本文を抽出
# - robots.txt を尊重（robotparser）
# - レート制限（ドメインごとに 1 リクエスト/秒）
# - タイムアウト処理（30 秒）
# - JS レンダリングサイトはスキップ（ログに記録）

async def extract_content(url: str) -> str | None:
    """記事 URL から本文を抽出する。取得できない場合は None を返す。"""

async def extract_contents(
    session: AsyncSession,
    articles: list[NewsArticle],
) -> ContentExtractionResult:
    """複数記事の本文を一括取得し DB に保存する。"""
```

**実装上の注意: newspaper4k は同期ライブラリ**

newspaper4k は内部で同期 HTTP クライアント（`requests`）を使用する。
async 環境で使うため、以下のいずれかの設計を採用する:

- **方式 A（推奨）:** httpx で HTML を非同期取得 → newspaper4k の
  パーサー部分のみを `asyncio.to_thread()` で実行
  （ネットワーク I/O を async に保ちつつ、CPU バウンドの HTML パースを
  ワーカースレッドに逃がす）
- **方式 B:** newspaper4k の `Article.download()` + `Article.parse()` を
  まるごと `asyncio.to_thread()` でラップ
  （シンプルだがネットワーク I/O がスレッドプール経由になる）

スコープ外（Phase 3 で検討）:
- JavaScript レンダリングが必要なサイト（Playwright 等）
- ペイウォール回避
- CAPTCHA 対応

#### A-4. BaseAnalyzer のシグネチャ変更

`backend/app/services/ai_analyzer.py`:
```python
class BaseAnalyzer(abc.ABC):
    @abc.abstractmethod
    async def analyze(
        self,
        title: str,
        description: str | None,
        content: str | None = None,  # ← 追加（後方互換）
    ) -> AnalysisData:
```

`backend/app/services/gemini_analyzer.py`:
- プロンプトを拡張: `content` がある場合は全文ベースで分析
- トークン制限: 全文は先頭 8,000 文字に切り詰め（Gemini 2.0 Flash のコンテキストは
  十分だが、コスト最適化のため。具体的な値は実装時にテストして調整）
- `content` がない場合は既存の title + description フォールバック

#### A-5. スキーマ & レスポンスの更新

`backend/app/schemas/news.py` の `NewsResponse` に追加:
```python
content: str | None = None
content_fetched_at: datetime | None = None
```

#### A-6. フロントエンドの更新

- `types/generated.ts` を再生成（Step 0 のパイプラインを使用）
- 記事詳細ページ（`app/news/[id]/page.tsx`）に本文表示セクションを追加
- 本文未取得の場合は「本文を取得中...」または元の description を表示

#### A-7. テスト

- `content_extractor.py` のユニットテスト（モック HTTP レスポンス）
- `gemini_analyzer.py` の全文対応テスト（content あり/なしの両方）
- スケジューラーの新パイプラインの統合テスト
- マイグレーションのアップグレード/ダウングレード確認

### 完了条件

- [ ] 新規記事のフェッチ時に全文が自動取得される
- [ ] AI 分析が全文ベースで実行される（content がある場合）
- [ ] 記事詳細ページに本文が表示される
- [ ] robots.txt を尊重し、レート制限が機能する
- [ ] content なしの記事でも既存フロー（title + description）が正常動作

---

## Step B-0: タスクキュー PoC

### 目的

現在のバックエンドは完全に async（asyncpg, AsyncSession, AsyncIOScheduler）で
構築されている。タスクキューライブラリとの統合を検証し、技術選定する。

### 候補

Celery は asyncio をネイティブサポートしていない。サードパーティの回避策
（celery-aio-pool、aio-celery）も開発が活発でなく、
既存の完全 async コードベースとの統合コストが高いため候補から除外する。

| 候補 | 特徴 |
|------|------|
| **arq** | asyncio ネイティブ、Redis ベース、軽量。graceful shutdown 時にジョブを自動再キュー。cron job 機能あり。モニタリングは arq-dashboard または自前 API |
| **taskiq** | asyncio ネイティブ、Celery ライクな API。taskiq-fastapi による FastAPI DI 統合。複数ブローカー対応。スケジュール機能あり（taskiq-crontab） |

### PoC 手順

各候補で以下の最小構成を実装し比較する:

1. Docker Compose にブローカー（Redis）+ ワーカーコンテナ追加
2. 既存の `fetch_news_for_keywords()` をタスクとして登録
3. API エンドポイントからタスクを投入し、結果を取得
4. エラー時のリトライ動作を確認
5. cron job でスケジュール実行を確認

### 判断基準

| 基準 | 重み |
|------|------|
| 既存 async サービスをそのまま呼べるか | 高 |
| DB セッション管理が自然にできるか（AsyncSession） | 高 |
| リトライ・タイムアウト・タスク状態管理の機能 | 中 |
| APScheduler を完全に置き換えられるか（スケジュール機能の有無） | 中 |
| graceful shutdown 時のジョブ再キュー動作 | 中 |
| モニタリング手段（ダッシュボード、ログ） | 中 |
| エコシステムの成熟度・ドキュメント | 中 |
| Docker Compose への統合が容易か | 低 |

### 成果物

- 技術選定レポート（各候補の検証結果 + 選定理由）
- 選定したライブラリの最小構成サンプル

### 期間

1 日以内

---

## Step B: タスクキュー分離

### 目的

重い処理（全文取得・AI 分析）を API プロセスから分離し、
スケーラビリティとレスポンス速度を確保する。

### アーキテクチャ（変更後）

```
[現在]
API プロセス (FastAPI)
  └── APScheduler（インプロセス）
        └── fetch → extract → analyze（すべて同一プロセス）

[Step B 後]
API プロセス (FastAPI)
  ├── タスク投入のみ
  └── タスク状態確認 API

ワーカープロセス
  ├── fetch_news_task        — RSS フェッチ + DB 保存
  ├── extract_content_task   — 全文取得
  ├── analyze_article_task   — AI 分析（単記事）
  └── cron: scheduled_pipeline — 定期的にタスクチェーンを投入

ブローカー (Redis)
  └── タスクキュー + スケジュール管理
```

APScheduler は**完全に削除**し、選定したタスクキューライブラリの
cron job 機能で定期実行を管理する。これにより:
- API プロセスからインプロセスのスケジューラーを排除
- スケジュール管理がワーカー側に一元化
- API プロセスの責務がリクエスト処理のみに集中

### タスク

#### B-1. Docker Compose の拡張

```yaml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "${REDIS_PORT:-6379}:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]

  worker:
    build:
      context: ./backend
    command: [タスクキューの起動コマンド — PoC 結果に依存]
    env_file:
      - .env
    environment:
      - REDIS_URL=redis://redis:6379/0  # Docker 内部 DNS
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
```

#### B-2. タスク定義

既存サービスをラップしてタスク化する:

```python
# backend/app/tasks/news.py
async def fetch_news_task(keyword_ids: list[int] | None = None) -> dict:
    """RSS フェッチ → 全文取得をチェーン実行する。"""

# backend/app/tasks/analysis.py
async def analyze_article_task(article_id: int) -> dict:
    """単一記事の AI 分析を実行する。"""

async def analyze_unprocessed_task() -> dict:
    """未分析の全記事を分析する。"""
```

#### B-3. APScheduler の削除とスケジュール移行

現在の `scheduler.py` を削除し、タスクキューの cron job に移行する:

```python
# arq の場合:
class WorkerSettings:
    cron_jobs = [
        cron(scheduled_pipeline, hour=None, minute=0)  # 毎時実行
    ]

# taskiq の場合:
@broker.task(schedule=[{"cron": "0 */3 * * *"}])  # 3時間ごと
async def scheduled_pipeline():
    ...
```

`backend/app/main.py` の `lifespan` から `start_scheduler()` / `stop_scheduler()` を削除。
`backend/app/services/scheduler.py` を削除。
`backend/requirements.txt` から `apscheduler` を削除。

#### B-4. API エンドポイントの変更

`POST /api/v1/news/fetch`:
- 現在: 同期的に実行して結果を返す（202 を返すが実際は完了待ち）
- 変更後: タスクをキューに投入し、タスク ID を即座に返す（真の非同期）

新規エンドポイント:
```
GET /api/v1/tasks/{task_id}
→ { "status": "pending" | "running" | "completed" | "failed",
     "result": { ... },
     "error": "..." }
```

#### B-5. フロントエンドの更新

- 「Fetch News」ボタン押下 → タスク ID を受け取る
- ポーリングまたは定期確認でタスク状態を表示
  （「フェッチ中...」「分析中...」「完了」「エラー」）
- タスク状態コンポーネントの作成

#### B-6. モニタリング

PoC 結果に基づき導入:
- arq の場合: arq-dashboard または structlog ベースのログ + 管理 API
- taskiq の場合: structlog ベースのログ + 管理 API

#### B-7. テスト

- タスク単体テスト（モックブローカー）
- タスクチェーンのテスト（fetch → extract → analyze）
- API からのタスク投入 → 状態確認の E2E テスト
- ワーカー停止時の graceful shutdown + ジョブ再キュー確認

### 完了条件

- [ ] 全文取得・AI 分析がワーカープロセスで非同期実行される
- [ ] API のレスポンス時間がタスク実行時間に依存しない
- [ ] `POST /news/fetch` が真の非同期（タスク ID 即時返却）
- [ ] `GET /tasks/{task_id}` でタスク状態を確認できる
- [ ] フロントエンドにタスク状態の表示がある
- [ ] APScheduler が完全に削除され、cron job でスケジュール実行される

---

## Step C: pgvector セマンティック検索

### 目的

キーワード完全一致ではなく、意味的に関連する記事を検索できるようにする。

### アーキテクチャ

```
記事保存時:
  content / title → Gemini Embedding API → Vector(768) → DB 保存

検索時:
  検索クエリ → Gemini Embedding API → ベクトル化
    → pgvector cosine similarity 検索 → 類似記事リスト

詳細ページ:
  記事の embedding → pgvector 近傍検索 → 関連記事セクション
```

### タスク

#### C-1. Docker イメージの変更

```yaml
# docker-compose.yml
db:
  image: pgvector/pgvector:pg16  # postgres:16-alpine から変更
```

既存ボリュームとの互換性:
- `pgvector/pgvector:pg16` は PostgreSQL 16 ベース → 互換性あり
- 初回起動時に `CREATE EXTENSION IF NOT EXISTS vector;` を実行（マイグレーション内）

#### C-2. エンベディングサービスの実装

`backend/app/services/embedding.py`:

```python
# Gemini text-embedding-004 (768 次元)
EMBEDDING_MODEL = "text-embedding-004"
EMBEDDING_DIMENSION = 768

async def generate_embedding(text: str) -> list[float]:
    """テキストからエンベディングベクトルを生成する。"""

async def generate_embeddings_batch(
    texts: list[str], batch_size: int = 100
) -> list[list[float]]:
    """バッチでエンベディングを生成する。"""
```

入力テキスト:
- `title_original` + `content`（本文がある場合）
- `content` がない場合は `title_original` + `description_original`

#### C-3. NewsArticle モデルの拡張

Alembic マイグレーション:
```sql
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE news_articles
ADD COLUMN embedding vector(768);

-- インデックスは記事数が増えてから追加
-- CREATE INDEX idx_news_embedding ON news_articles
--   USING hnsw (embedding vector_cosine_ops);
```

#### C-4. エンベディング生成の統合

記事分析完了時に自動でエンベディングを生成する:
- タスクキュー（Step B）に `generate_embedding_task` を追加
- 分析タスク完了後にチェーンで実行

#### C-5. 既存記事のバックフィル

タスクキュー（Step B）を使って一括処理:

```python
async def backfill_embeddings_task():
    """embedding が NULL の記事に対してエンベディングを生成する。"""
```

- バッチサイズ: 100 記事ずつ
- レート制限: Gemini Embedding API の制限に準拠

#### C-6. API エンドポイント

新規:
```
GET /api/v1/news/{id}/similar?limit=5
→ 類似記事リスト（cosine similarity 降順）
```

既存の `GET /api/v1/news` に検索パラメータ追加:
```
GET /api/v1/news?q=quantum+computing&searchMode=semantic
→ セマンティック検索結果
```

`searchMode` パラメータ:
- `keyword`（デフォルト）: 既存のキーワードフィルター
- `semantic`: ベクトル類似度検索

#### C-7. フロントエンドの更新

- 記事詳細ページに「関連記事」セクション追加
- 検索バーに `searchMode` 切り替え（キーワード / セマンティック）
- 型再生成（Step 0 のパイプライン使用）

#### C-8. パフォーマンスとインデックス

初期段階（記事数 < 数千件）:
- 逐次スキャンで十分。インデックスなし。

スケールアウト時:
- HNSW インデックス追加（精度とのトレードオフが良い）
- `lists` パラメータは記事数の平方根を目安に設定

距離関数の選定:
- `vector_cosine_ops`（コサイン類似度）を使用
  → 文書長の影響を受けにくく、テキストエンベディングに適切

#### C-9. テスト

- エンベディング生成のユニットテスト（モック API）
- 類似記事検索の統合テスト
- バックフィルタスクのテスト
- 検索精度の定性的な確認

### 完了条件

- [ ] 記事保存時にエンベディングが自動生成される
- [ ] `GET /api/v1/news/{id}/similar` で類似記事が返る
- [ ] 検索バーでセマンティック検索ができる
- [ ] 既存記事のバックフィルが完了している
- [ ] 関連記事セクションが詳細ページに表示される

---

## 全ステップ共通の方針

### テスト

- 各ステップ完了時に既存テスト + 新規テストが全て通ること
- バックエンド: pytest（モック + DB 統合テスト）
- フロントエンド: TypeScript コンパイル（`tsc --noEmit`）

### マイグレーション

- 全ての DB 変更は Alembic マイグレーション経由
- アップグレード/ダウングレードの両方を確認

### 型安全

- Step 0 のパイプラインを各ステップのスキーマ変更時に実行
- フロントエンドのコンパイルエラーを即座に検出

### 環境変数

新規追加分は `.env.example` と `backend/app/config.py` に反映:

```env
# Step A
CONTENT_MAX_LENGTH=8000             # AI 分析に渡す本文の最大文字数

# Step B
REDIS_URL=redis://localhost:6379/0  # タスクキューブローカー（ローカル開発用）
                                    # Docker 内部では docker-compose.yml で
                                    # redis://redis:6379/0 にオーバーライド

# Step C
EMBEDDING_MODEL=text-embedding-004
EMBEDDING_DIMENSION=768
```
