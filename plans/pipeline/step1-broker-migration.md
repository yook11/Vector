# Step 1: ブローカー変更 — ListQueueBroker → RedisStreamBroker

親ドキュメント: [pipeline_architecture.md](../pipeline_architecture.md)

## 目的

既存のタスク構造を維持したまま、ブローカーだけを差し替える。
タスク分割（Step 2）の前提基盤を整える。

## 変更の原則

- タスク定義（`@broker.task`）、タスク呼び出し（`.kiq()`）、スケジューラーは変更しない
- ブローカー初期化コードと Docker Compose（worker のみ）を変更
- 既存の `fetch_and_analyze_task` はそのまま動作すること
- scheduler コマンドは変更不要 — `--ack-type` は worker（consumer）側のオプション。scheduler は `broker.kick()` → `XADD` で Stream に投入するだけなので影響なし

---

## 変更内容

### 1. `backend/app/tasks/taskiq_worker.py` — ブローカー初期化

```python
# 変更前 (L77-90)
from taskiq_redis import ListQueueBroker

broker = (
    ListQueueBroker(url=settings.redis_url)
    .with_result_backend(
        RedisAsyncResultBackend(
            redis_url=settings.redis_url,
            result_ex_time=3600,
        )
    )
    .with_middlewares(
        SimpleRetryMiddleware(default_retry_count=0)
    )
)

# 変更後
from taskiq_redis import RedisStreamBroker

broker = (
    RedisStreamBroker(
        url=settings.redis_url,
        idle_timeout=600_000,    # 10分: 未 Ack メッセージの回収閾値
        maxlen=10_000,           # Stream メモリ制御
    )
    .with_result_backend(
        RedisAsyncResultBackend(
            redis_url=settings.redis_url,
            result_ex_time=3600,
        )
    )
    .with_middlewares(
        SimpleRetryMiddleware(default_retry_count=0)
    )
)
```

### 2. `docker-compose.yml` — worker コマンド

```yaml
# 変更前 (L103)
command: taskiq worker app.tasks.taskiq_worker:broker app.tasks.taskiq_worker

# 変更後
command: >
  taskiq worker
  app.tasks.taskiq_worker:broker
  app.tasks.taskiq_worker
  --ack-type when_executed
```

### 3. `backend/requirements.txt` — バージョン確認

`taskiq-redis>=1.0,<2` — 現在 1.2.2 がインストール済み。`RedisStreamBroker` は利用可能（確認済み）。変更不要。

---

## 検証項目

- [ ] ローカルで `taskiq worker` が起動し、`RedisStreamBroker` で接続できること
- [ ] `POST /api/v1/news/fetch` でタスクがエンキュー・実行されること
- [ ] scheduler がcronスケジュールでタスクを投入できること
- [ ] `redis-cli XLEN taskiq` で Stream にメッセージが入ること
- [ ] 既存テスト（`tests/test_taskiq_worker.py`）が通ること
  - テストは `fetch_and_analyze_task` を直接呼び出し、broker を経由しない。`ListQueueBroker` への直接参照もないため import 変更の影響なし
- [ ] テスト環境（Redis 未起動）で `from app.tasks.taskiq_worker import broker` が ConnectionError にならないこと
  - `RedisStreamBroker` コンストラクタは lazy 接続（`startup()` まで TCP 接続しない）の想定だが、明示的に確認する

---

## 切り替え手順

1. worker / scheduler を停止
2. 旧キューの残存タスクを確認: `redis-cli LLEN taskiq:fetch_and_analyze`
   - 0 なら問題なし → Step 4 へ
   - \>0 なら残存タスクの完了を待つか、手動で消化
3. 旧キューをクリーンアップ: `redis-cli DEL taskiq:fetch_and_analyze`
   - **`FLUSHDB` は使わない** — result backend やセッション等の他キーを巻き込む
4. コード変更をデプロイ（broker 変更 + `--ack-type`）
5. worker / scheduler を起動
6. 検証: `redis-cli XLEN taskiq` で Stream にメッセージが入ることを確認

> **Note**: ListQueueBroker（LIST 型）と RedisStreamBroker（STREAM 型）はキー名空間が異なるため、同一 Redis インスタンス内で衝突しない。ただし旧 List キーは誰にも消費されず残り続けるため、Step 3 で明示的に削除する。

---

## リスク

- **低**: コンストラクタ互換性は検証済み。`.with_result_backend()` / `.with_middlewares()` は `AsyncBroker` から継承
- **低**: `LabelScheduleSource` は `AsyncBroker` を受け取る設計。MRO 確認済み
- **注意**: 旧 List キーが切り替え後も Redis に残る可能性がある。切り替え手順の Step 3 で削除すること
