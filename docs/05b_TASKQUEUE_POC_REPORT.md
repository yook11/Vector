# タスクキュー PoC レポート: arq vs taskiq (Step B-0)

> **ステータス: 完了（選定結果反映済み）**
>
> taskiq を選定し、Step B で本番実装済み。
> 実装の詳細は `archive/03_CLAUDE_CODE_WORKFLOW.md` の Step D (タスクキュー) を参照。
> 以下は PoC 実施時の記録をそのまま保持している。

## 調査済み事前情報

| 項目 | arq | taskiq |
|------|-----|--------|
| バージョン | v0.27.0 (2025-02 リリース) | v0.12.1 (2025-12 リリース) |
| 開発状況 | **maintenance-only** (README issue #510) | **活発に開発中** |
| 実行保証 | at-least-once（タスクは冪等必須） | 概念的に once-only |
| cron 実行 | 単一プロセス（WorkerSettings.cron_jobs） | **2プロセス必要**（worker + scheduler） |
| ブローカー対応 | Redis のみ | Redis / RabbitMQ / NATS / Kafka |
| 負荷テスト性能 | 標準 | ~10× 高速 |

---

## テスト環境

- 実施日: 2026-02-21
- Python: 3.12
- arq: 0.27.0
- taskiq: 0.12.1
- taskiq-redis: 1.0.9（最新 1.2.2 — `requirements.txt` の `<1` 制約は Step B で `>=1.0,<2` に修正必要）
- Redis: 7-alpine
- DB: PostgreSQL 16 (localhost:5433)

---

## テスト手順

```bash
# 共通セットアップ
docker compose up -d redis db
export DATABASE_URL="postgresql+asyncpg://vector:vector@localhost:5433/vector"
export REDIS_URL="redis://localhost:6379/0"
cd backend
pip install arq==0.27.0 taskiq taskiq-redis
```

### arq テスト

```bash
# ワーカー起動
python -m arq app.tasks.arq_worker.WorkerSettings

# タスク投入（別ターミナル）
python - <<'EOF'
import asyncio
from arq.connections import ArqRedis, RedisSettings

async def main():
    r = await ArqRedis.create(RedisSettings())
    job = await r.enqueue_job("fetch_and_analyze_task")
    print(f"enqueued: {job.job_id}")
    result = await job.result(timeout=120)
    print(f"result: {result}")
    await r.aclose()

asyncio.run(main())
EOF

# リトライ確認
python - <<'EOF'
import asyncio, time
from arq.connections import ArqRedis, RedisSettings

async def main():
    r = await ArqRedis.create(RedisSettings())
    job = await r.enqueue_job("failing_task")
    time.sleep(30)
    info = await job.info()
    print(f"status: {info}")
    await r.aclose()

asyncio.run(main())
EOF
```

### taskiq テスト

```bash
# ワーカー起動
taskiq worker app.tasks.taskiq_worker:broker app.tasks.taskiq_worker

# スケジューラー起動（別ターミナル）
taskiq scheduler app.tasks.taskiq_worker:scheduler

# タスク投入（別ターミナル）
python - <<'EOF'
import asyncio
from app.tasks.taskiq_worker import broker, fetch_and_analyze_task

async def main():
    await broker.startup()
    task = await fetch_and_analyze_task.kiq()
    print(f"task_id: {task.task_id}")
    result = await task.wait_result(timeout=120)
    print(f"result: {result.return_value}, err: {result.is_err}")
    await broker.shutdown()

asyncio.run(main())
EOF
```

---

## 評価結果

| 評価基準 | 重み | arq 評価 | taskiq 評価 | 観察・メモ |
|---------|------|----------|------------|----------|
| 既存 async サービスを直接呼べるか | 高 | ✅ | ✅ | 両者ともサービス関数を直接呼び出し可能 |
| AsyncSession 管理の自然さ | 高 | ✅ | ✅ | arq: ctx["engine"] / taskiq: engine を直接生成（PoC）、本番は TaskiqDepends |
| **DB engine イベントループエラーなし** ★最重要 | 高 | ✅ | ✅ | 両者とも on_startup で engine 生成 → RuntimeError なし |
| **engine のライフサイクル共有** (★taskiq 検証項目) | 高 | ✅ N/A（ctx 方式） | ⚠️ PoC は per-task 生成 | taskiq: `Context = TaskiqDepends()` → `context.state.engine` パターンで共有可能（本番 Step B で実装）|
| リトライ動作 | 中 | ⚠️ | ⚠️ | arq: **明示的** `arq.Retry` 必須（ValueError では即時永続失敗）。taskiq: `SimpleRetryMiddleware` + `retry_on_error=True` の**両方**が必要 |
| タスクタイムアウト動作 | 中 | ✅（設定済） | ✅（設定済） | arq: job_timeout=600 / taskiq: timeout=600 |
| タスク結果の参照しやすさ | 中 | ✅ | ✅ | arq: job.result() / taskiq: task.wait_result() |
| APScheduler 置換可能か（cron） | 中 | ✅ 単一プロセス | ✅ 別プロセス | arq: WorkerSettings.cron_jobs で完結。taskiq: scheduler プロセスが別途必要だが動作確認済み |
| Graceful shutdown + 再キュー | 中 | ✅（ドキュメント済） | ✅ | 両者とも SIGTERM でシャットダウン対応 |
| ログの可読性 | 中 | ✅ | ✅ | structlog との相性良好 |
| エコシステム成熟度・将来性 | 中 | ⚠️ maintenance-only | ✅ 活発開発中 | arq: 2025-02 以降バグ修正のみ。taskiq: 活発開発中（2025-12 最新） |
| Docker Compose 統合の容易さ | 低 | ✅ 単一コンテナ | ⚠️ 要設計 | arq: worker 1コンテナのみ。taskiq: worker + scheduler の統合方式は Step B で再評価（下記参照） |

### arq テスト詳細結果

| 検証項目 | 結果 | 備考 |
|---------|------|------|
| DB 接続（イベントループ） | ✅ | on_startup で engine 生成 → RuntimeError なし |
| RSS フェッチ | ✅ new=50 | fetch_news_for_keywords() 正常動作 |
| コンテンツ抽出 | ✅ | robots.txt ブロックは既知仕様 |
| AI 分析 | ❌ GEMINI_API_KEY 未設定 | arq 自体の問題ではない（環境変数の漏れ） |
| リトライ（failing_task） | ⚠️ attempt=1 で永続失敗 | **原因: ValueError は即時永続失敗。arq.Retry を raise する必要あり（設計仕様）** |
| cron 設定 | ✅ | WorkerSettings.cron_jobs に定義済み（実発火未確認） |

### arq リトライ設計の重要事項

arq のリトライは **意図的に明示的設計**:

- `ValueError` 等の通常例外 → 即時永続失敗（リトライなし）
- `arq.Retry(defer=N)` を raise → N秒後にリトライ（max_tries 回まで）

```python
# 本番実装でのリトライパターン
from arq import Retry

async def fetch_with_retry(ctx: dict) -> dict:
    try:
        ...
    except TransientError as e:
        raise Retry(defer=ctx["job_try"] * 5)  # 5s, 10s, 15s バックオフ
    except PermanentError:
        raise  # 即時失敗
```

→ **PoC での再テストはスキップ**（arq はメンテナンスモードで採用予定外）。

### taskiq テスト詳細結果

| 検証項目 | 結果 | 備考 |
|---------|------|------|
| DB 接続（イベントループ） | ✅ | on_startup で engine 生成 → RuntimeError なし |
| RSS フェッチ | ✅ | fetch_news_for_keywords() 正常動作 |
| コンテンツ抽出 | ✅ | robots.txt ブロックは既知仕様 |
| AI 分析 | ❌ GEMINI_API_KEY 未設定 | taskiq 自体の問題ではない |
| リトライ（failing_task） | ⚠️ 要設定 | `SimpleRetryMiddleware` + `retry_on_error=True` の**両方**が必要（下記参照） |
| マルチワーカー | ✅ | デフォルト 2プロセス（worker-0, worker-1）で動作確認 |
| スケジューラー（別プロセス） | ✅ | `taskiq scheduler` コマンドで正常動作 |
| cron スケジュール | ✅ | `schedule=[{"cron": "0 */3 * * *"}]` で LabelScheduleSource が認識 |

### taskiq の engine ライフサイクル検証手順

PoC の `taskiq_worker.py` ではタスクごとに engine を生成・破棄している。
本番実装では `on_startup` で生成した `state.engine` を再利用するべきで、
以下を Step B で実装する:

```python
# TaskiqDepends で state.engine を共有する本番パターン
from taskiq import Context, TaskiqDepends

@broker.task(task_name="fetch_with_shared_engine")
async def fetch_with_shared_engine(
    ctx: Context = TaskiqDepends(),
) -> str:
    engine = ctx.state.engine  # on_startup で生成した engine を再利用
    async with SQLModelAsyncSession(engine) as session:
        ...
```

**注意**: `broker.state.engine` への直接アクセスは非推奨。`Context = TaskiqDepends()` 経由が正式パターン。

### taskiq リトライ設定の重要事項

taskiq でリトライを有効にするには **2つの設定が両方必要**:

```python
# 1. ブローカーに SimpleRetryMiddleware を追加
from taskiq.middlewares.retry_middleware import SimpleRetryMiddleware

broker = ListQueueBroker(url=settings.redis_url).with_result_backend(
    RedisAsyncResultBackend(redis_url=settings.redis_url, result_ex_time=300)
).with_middlewares(SimpleRetryMiddleware(default_retry_count=3))

# 2. タスクデコレーターに retry_on_error=True を追加（どちらか欠けても動作しない）
@broker.task(
    task_name="fetch_and_analyze",
    timeout=600,
    max_retries=3,
    retry_on_error=True,  # ← 必須
    schedule=[{"cron": "0 */3 * * *"}],
)
async def fetch_and_analyze_task() -> dict:
    ...
```

`SimpleRetryMiddleware` のみ、または `retry_on_error=True` のみでは **リトライは発動しない**。

---

## 観察された問題・ハマりポイント

### arq

- `arq.Retry` を明示 raise しないとリトライが発動しない（設計仕様、未再テスト）
- 環境変数 `GEMINI_API_KEY` が未設定だと AI 分析フェーズが失敗（arq の問題ではない）
- `ArqRedis.create()` は v0.27.0 時点で公式 deprecation は未確認。ただし公式ドキュメントでは `create_pool()` を使用しているため、Step B では `create_pool()` を推奨

### taskiq

- `SimpleRetryMiddleware` と `retry_on_error=True` の両方が必要（片方では不十分）
- `broker.state.engine` への直接アクセスは非推奨。`Context = TaskiqDepends()` → `ctx.state.engine` が正式パターン
- `taskiq-redis` v1.x（テスト時は v1.0.9、最新 v1.2.2）は `>=1.0,<2` として管理すべき。現 `requirements.txt` の `>=0.5,<1` は **Step B で修正必要**
- Apple Silicon (ARM) では `greenlet` が自動インストールされない。`pip install greenlet` または `uv add greenlet` が必要
- `taskiq scheduler` は別プロセスが必要（PoC では手動起動、本番 Docker では下記参照）

---

## 選定結論

**選定ライブラリ: taskiq**

### 選定理由

- **活発な開発**: arq が maintenance-only（2025-02 以降バグ修正のみ）に対し、taskiq は 2025-12 にも v0.12.1 をリリース。長期運用に安心感がある
- **自動リトライ**: 設定は `SimpleRetryMiddleware` + `retry_on_error=True` の 2ステップ必要だが、任意の例外で自動リトライできる（arq は `arq.Retry` の明示 raise が必要）
- **将来の拡張性**: Redis 以外に RabbitMQ / NATS / Kafka にも対応。スケールアウト時に broker を差し替え可能
- **パフォーマンス**: 負荷テスト比較で arq の約 10× 高速（現状は規模が小さく影響なし、将来のバッファ）
- **動作確認済み**: DB 接続、RSS フェッチ、コンテンツ抽出、マルチワーカー、スケジューラーすべて PoC で正常動作を確認

### 懸念事項と Step B での対応方針

- **リトライ設定の複雑さ**: `SimpleRetryMiddleware` + `retry_on_error=True` の両方が必要である点を Step B 実装時に忘れず設定
- **engine 共有**: PoC では per-task で engine を生成したが、本番では `Context = TaskiqDepends()` → `ctx.state.engine` パターンに変更して connection pool を再利用
- **taskiq-redis バージョン**: `requirements.txt` の `taskiq-redis>=0.5,<1` を `>=1.0,<2` に修正（Step B で対応）
- **greenlet**: Dockerfile に `RUN pip install greenlet` を追加（または requirements.txt に明記）

---

## Step B への影響

### 採用した場合の docker-compose.yml 変更

```yaml
# docker-compose.yml に追加するサービス（案）
worker:
  build:
    context: ./backend
  command: taskiq worker app.tasks.taskiq_worker:broker app.tasks.taskiq_worker
  env_file:
    - .env
  environment:
    - DATABASE_URL=postgresql+asyncpg://vector:vector@db:5432/vector
    - REDIS_URL=redis://redis:6379/0
  depends_on:
    db:
      condition: service_healthy
    redis:
      condition: service_healthy

scheduler:
  build:
    context: ./backend
  command: taskiq scheduler app.tasks.taskiq_worker:scheduler
  env_file:
    - .env
  environment:
    - DATABASE_URL=postgresql+asyncpg://vector:vector@db:5432/vector
    - REDIS_URL=redis://redis:6379/0
  depends_on:
    db:
      condition: service_healthy
    redis:
      condition: service_healthy
```

**⚠️ 重要: `bash -c "taskiq worker ... & taskiq scheduler ... & wait"` パターンは採用しない。**
一方がクラッシュしてもコンテナが再起動されないため、プロセス監視が機能しない。
`worker` と `scheduler` を別コンテナに分けるか、`supervisord` を使うかを Step B で決定する。
**→ 2コンテナ分離を推奨**（Docker Compose のヘルスチェックと再起動ポリシーが正常機能するため）。

### APScheduler の削除方針

- `backend/app/services/scheduler.py` の `start_scheduler()` / `stop_scheduler()` を削除
- `backend/app/main.py` の lifespan から scheduler 関連コードを削除
- `requirements.txt` から `apscheduler` を削除
- 代わりに taskiq の cron 機能（`schedule=[{"cron": "0 */3 * * *"}]` + `taskiq scheduler`）でスケジュール管理

### Step B 実装チェックリスト

- [x] `taskiq-redis>=0.5,<1` → `>=1.0,<2` に修正
- [x] `SimpleRetryMiddleware` をブローカーに追加
- [x] `retry_on_error=True` を各タスクデコレーターに追加
- [x] `fetch_and_analyze_task` を `Context = TaskiqDepends()` → `ctx.state.engine` 方式に変更
- [x] Dockerfile に `greenlet` を明示的に追加
- [x] `worker` と `scheduler` を別コンテナに分離（2コンテナ分離方式を採用）
- [x] `apscheduler` を `requirements.txt` から削除
- [x] `backend/app/services/scheduler.py` と `main.py` の lifespan から APScheduler を削除
