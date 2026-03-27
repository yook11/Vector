# パイプラインアーキテクチャ刷新 — 設計ドキュメント v4

## 経緯

C-3（レガシーカラム除去）の計画レビュー中に、根本的なアーキテクチャ問題が判明。カラムを消すだけでは不十分で、パイプライン自体の再設計が必要と判断。C-3 は新パイプライン完成後に実施する。

v2 の設計レビューで「DB にパイプラインの処理状態を持たせるのはエンティティの関心ではない」という根本方針を確立。3カラム方式（content_status / analysis_status / embedding_status）を撤回し、ブローカー（Redis）に状態管理を委譲する設計に刷新した。

v3 → v4: ブローカー変更の技術検証が完了。RedisStreamBroker の内部実装（ソースコード）を直接確認し、Ack・リトライ・クラッシュ復旧の挙動を検証済み。影響範囲の洗い出しも完了。

---

## 設計原則

1. **DB はドメインの事実だけ持つ** — 「本文がある/ない」「分析済み/未分析」はデータの有無で判定。パイプラインの処理状態（pending/queued/fetching...）は DB に持たない
2. **パイプラインの状態は Redis が管理** — タスクのキュー、リトライ、クラッシュ復旧は RedisStreamBroker の Ack 機能で解決
3. **永続的失敗は発生した場所で対処** — 下流に問題を流さない。fetch_content が品質を保証し、分析フェーズは入力の品質を前提にできる

---

## 現状のアーキテクチャ（問題）

```
scheduler (30分ごと)
  → fetch_and_analyze (1つの巨大バッチタスク, timeout=1800s)
    → Phase 1-2: RSS/HN メタデータ取得
    → Phase 3: 本文取得 (SELECT WHERE content_fetched_at IS NULL → ループで逐次処理)
    → Phase 4: AI分析 (SELECT WHERE no ArticleAnalysis → 逐次処理, 4s間隔)
    → Phase 5: エンベディング生成 (バッチ10件, 8s間隔)
    → Phase 6: 重複検出
```

問題点:
- Phase 3/4/5 がバッチの中のループなので、リトライ状態を DB に持たせる必要があった
- Phase 4 で RateLimitError (429) が発生すると残り全件が未分析のまま放棄される
- Phase 4 が途中で止まると Phase 5/6 も空振りになる（新規分析がないので埋め込む対象がない）
- DB に処理状態（content_fetch_attempts 等）を持たせて技術的負債化
- ListQueueBroker は Ack をサポートしない。ワーカーがクラッシュするとタスクが消失する

---

## 目標のアーキテクチャ

```
scheduler (30分ごと)
  → fetch_metadata           — バッチでRSS/HN取得
    → dispatch_pending       — 未処理記事を SELECT → 個別タスクとしてエンキュー
      → fetch_content(id)    — 個別タスク、Redis がリトライ・復旧管理
        → analyze_article(id)  — 成功時にチェーン
          → generate_embedding(id) — 成功時にチェーン
```

責務の分離:
- fetch_metadata: ソースからメタデータを収集する
- dispatch_pending: 未処理記事を見つけてキューに振り分ける
- fetch_content / analyze_article / generate_embedding: 1記事ずつ独立して処理する

---

## ブローカー: ListQueueBroker → RedisStreamBroker

### 変更理由

ListQueueBroker は Ack をサポートしない。ワーカーがクラッシュするとタスクが消失する。
RedisStreamBroker は Redis Streams + Consumer Groups を使い、Ack による確実なタスク管理を提供する。

### 技術検証結果（ソースコード直接確認済み）

| 検証項目 | 結果 |
|---|---|
| コンストラクタ互換性 | `url` 第一引数は共通。`.with_result_backend()` / `.with_middlewares()` チェーンも継承元 `AsyncBroker` から利用可能 |
| `--ack-type` CLI オプション | `{when_received, when_executed, when_saved}` の3種。`taskiq worker --help` で確認済み |
| SimpleRetryMiddleware 互換性 | `on_error()` で `AsyncKicker.kiq()` により新メッセージを投入する方式。Ack とは独立して動作する（後述） |
| LabelScheduleSource 互換性 | `TaskiqScheduler` は `AsyncBroker` を受け取る設計。MRO: `RedisStreamBroker → BaseRedisBroker → AsyncBroker`。互換性あり |
| Redis バージョン要件 | Redis Streams は 5.0 で導入。Vector は `redis:7-alpine` なので問題なし |
| taskiq-redis バージョン | `1.2.2` がインストール済み。`RedisStreamBroker` クラスは利用可能（`dir()` で確認済み） |

### ブローカー設定

```python
# 変更前
from taskiq_redis import ListQueueBroker
broker = (
    ListQueueBroker(url=settings.redis_url)
    .with_result_backend(RedisAsyncResultBackend(...))
    .with_middlewares(SimpleRetryMiddleware(default_retry_count=0))
)

# 変更後
from taskiq_redis import RedisStreamBroker
broker = (
    RedisStreamBroker(
        url=settings.redis_url,
        idle_timeout=600_000,    # 10分（デフォルト）。未 Ack メッセージの回収閾値
        maxlen=10_000,           # Stream の最大長。メモリ消費を制御
        approximate=True,        # maxlen の厳密なトリムを避ける（パフォーマンス優先）
    )
    .with_result_backend(RedisAsyncResultBackend(...))
    .with_middlewares(SimpleRetryMiddleware(default_retry_count=0))
)
```

コンストラクタの主要パラメータ（ソースコード確認済み）:

| パラメータ | デフォルト | 説明 |
|---|---|---|
| `queue_name` | `"taskiq"` | Redis Stream のキー名 |
| `consumer_group_name` | `"taskiq"` | Consumer Group 名 |
| `consumer_name` | `None` → UUID 自動生成 | Consumer 識別子 |
| `idle_timeout` | `600000` (10分) | 未 Ack メッセージの回収閾値 (ms) |
| `maxlen` | `None` (無制限) | Stream の最大長 |
| `approximate` | `True` | maxlen のトリミングを近似的に行う |
| `unacknowledged_batch_size` | `100` | 1回で回収する未 Ack メッセージ数 |
| `mkstream` | `True` | Stream が存在しなければ自動作成 |
| `xread_block` | `2000` (2秒) | xreadgroup のブロック時間 (ms) |

### Docker Compose

```yaml
# worker
worker:
  command: >
    taskiq worker
    app.tasks.pipeline_tasks:broker
    app.tasks.pipeline_tasks
    --ack-type when_executed

# scheduler（変更なし）
scheduler:
  command: taskiq scheduler app.tasks.pipeline_tasks:scheduler
```

### Ack タイプ: `when_executed` を選択する理由

Receiver.callback() のソースコードから確認した実行順序:

```
1. run_task()        — タスク実行（例外は BaseException でキャッチ → result.error に格納）
2. message.ack()     — when_executed: ここで xack（元の msg_id を Ack）
3. post_execute      — ミドルウェアの後処理
4. on_error()        — SimpleRetryMiddleware: AsyncKicker.kiq() で新メッセージ投入
5. set_result()      — result_backend に結果保存
6. message.ack()     — when_saved: ここで xack
```

`when_executed` を選ぶ理由:
- Ack (Step 2) → リトライ投入 (Step 4) の順序が保証される。Ack とリトライは二重に動かない
- `when_saved` はデフォルトだが、result_backend の保存が完了するまで Ack されない。result_backend が不要な場面では無駄に Ack が遅れる
- `when_received` はタスク実行前に Ack するため、クラッシュ時にタスクが消失する（ListQueueBroker と同じ問題）

### クラッシュ復旧の仕組み（xautoclaim）

`listen()` メソッドの毎ポーリングサイクルで実行される:

```python
# RedisStreamBroker.listen() 内部（ソースコード確認済み）
# Step 1: 新メッセージ取得
fetched = await redis_conn.xreadgroup(consumer_group, consumer_name, {stream: ">"}, ...)
for stream, msg_list in fetched:
    for msg_id, msg in msg_list:
        yield AckableMessage(data=msg[b"data"], ack=ack_generator(msg_id, stream))

# Step 2: 未 Ack メッセージの回収（排他ロック付き）
for stream in streams:
    lock = redis_conn.lock(f"autoclaim:{consumer_group}:{stream}", ...)
    if await lock.locked():
        continue  # 他のワーカーが回収中ならスキップ
    async with lock:
        pending = await redis_conn.xautoclaim(
            name=stream,
            groupname=consumer_group,
            consumername=consumer_name,
            min_idle_time=self.idle_timeout,  # 10分
            count=self.unacknowledged_batch_size,  # 100
        )
        for msg_id, msg in pending[1]:
            yield AckableMessage(data=msg[b"data"], ack=ack_generator(msg_id, stream))
```

復旧フロー:
1. ワーカー A がタスクを取り出す → Redis Stream 上で「保留中」（Pending Entry）
2. ワーカー A がクラッシュ → Ack が届かない → メッセージは Pending のまま残る
3. 10分後（idle_timeout）、ワーカー B（または再起動した A）が `xautoclaim` で回収
4. 回収されたメッセージが再処理される

排他ロック (`redis_conn.lock`) により、複数ワーカーが同時に xautoclaim するのを防止。

v2 の `recover_interrupted()` (startup 時リセット) は完全に不要。Consumer Group 設定も `startup()` で自動作成される。

### SimpleRetryMiddleware との相互作用

ソースコード確認済み。リトライ時の流れ:

```
1. fetch_content(id=42) 実行 → TemporaryFetchError で失敗
2. run_task() が例外をキャッチ → result.error に格納
3. --ack-type when_executed → 元の msg_id を xack（Ack 完了）
4. SimpleRetryMiddleware.on_error() 発火
5. AsyncKicker(...).with_task_id(message.task_id).kiq()
   → xadd で新しい Stream エントリ追加（新しい msg_id が振られる）
6. 新エントリがワーカーに配達 → リトライ実行
```

重要なポイント:
- **同じ task_id だが、Redis Stream 上では別の msg_id を持つ別エントリ。** Ack は msg_id ベースなので task_id の重複は問題にならない
- **Ack → リトライ投入の順序。** 元メッセージが先に Ack されてからリトライメッセージが投入される。二重実行のリスクなし
- **リトライカウントは `message.labels["_retries"]` で管理。** DB カラム不要

### リトライ

SimpleRetryMiddleware はそのまま使用:

```python
broker.with_middlewares(SimpleRetryMiddleware(default_retry_count=0))
# default_retry_count=0: 全タスクが max_retries を明示宣言する前提

# タスク側で宣言
@broker.task(max_retries=3, retry_on_error=True)
async def fetch_content(article_id: int, ...):
    ...
# → 初回 + リトライ3回 = 最大4回実行
```

---

## DB スキーマ変更

### 追加するカラム: 1本のみ

```python
# NewsArticle
skip_content_fetch: bool = Field(default=False)
```

「この記事の本文取得は永続的に失敗した。再試行しない。」を表すフラグ。

### 追加しないもの

- content_status / analysis_status / embedding_status → パイプライン状態は Redis が管理
- content_fetch_attempts / content_fetch_failures → リトライ回数は SimpleRetryMiddleware が管理
- skip_analysis / skip_embedding → 分析・埋め込みの永続的失敗は別の方法で対処（後述）

### 「処理済み」の判定方法

既存データの有無で判定する。追加カラム不要:

| フェーズ | 「処理済み」の判定 |
|---|---|
| 本文取得 | `original_content IS NOT NULL` |
| AI分析 | `article_analyses` レコードが存在 |
| 埋め込み | `article_analyses.embedding IS NOT NULL` |

### インデックス

```sql
CREATE INDEX idx_content_fetch_pending
    ON news_articles (skip_content_fetch)
    WHERE original_content IS NULL AND skip_content_fetch = FALSE;
```

dispatch_pending 用の部分インデックス。記事数が増えて遅くなったら EXPLAIN ANALYZE で確認して追加。

---

## dispatch の判定ロジック

```sql
-- 本文取得の dispatch
SELECT id FROM news_articles
WHERE original_content IS NULL
  AND skip_content_fetch = FALSE;

-- AI分析の dispatch（障害復旧・バックフィル用）
SELECT na.id FROM news_articles na
LEFT JOIN article_analyses aa ON aa.news_article_id = na.id
WHERE na.original_content IS NOT NULL
  AND aa.id IS NULL;

-- 埋め込みの dispatch（障害復旧・バックフィル用）
SELECT na.id FROM news_articles na
JOIN article_analyses aa ON aa.news_article_id = na.id
WHERE aa.embedding IS NULL;
```

注: 分析・埋め込みの dispatch はチェーン方式（前フェーズの成功時に直接エンキュー）で動作するため、定期的な SELECT は通常不要。障害復旧時やバックフィル時に使う。

---

## エラーハンドリング戦略

### 原則: エラーの種類で対処を分ける

```
永続的失敗（何回やっても同じ）→ 即座に対処、リトライしない
一時的失敗（リトライで回復）  → raise してブローカーに委譲
```

### fetch_content のエラーハンドリング

```python
@broker.task(retry_on_error=True, max_retries=3)
async def fetch_content(article_id: int, context: Context = TaskiqDepends()):
    async with get_session() as session:
        article = await session.get(NewsArticle, article_id)

        # 冪等性: 既に取得済みならスキップ
        if article.original_content is not None:
            return

        try:
            content = await extract_content(article.original_url)

            # 品質ゲート
            if content is None or len(content.strip()) < MIN_CONTENT_LENGTH:
                article.skip_content_fetch = True
                await session.commit()
                return  # 分析に進まない

            article.original_content = content
            await session.commit()
            await analyze_article.kiq(article_id)

        except PermanentFetchError:    # 403, 404, robots.txt
            article.skip_content_fetch = True
            await session.commit()
            # raise しない → リトライ不要

        except TemporaryFetchError:    # 5xx, タイムアウト
            if _is_last_attempt(context):
                article.skip_content_fetch = True
                await session.commit()
            raise  # ブローカーにリトライさせる
```

fetch_content の責務:
1. URL から本文を取得する
2. 品質ゲート（最低文字数）を通す
3. 通過した本文だけ DB に保存して次フェーズに渡す
4. 通過しなかったら skip_content_fetch = True

### analyze_article のエラーハンドリング

```python
@broker.task(retry_on_error=True, max_retries=3)
async def analyze_article(article_id: int, context: Context = TaskiqDepends()):
    async with get_session() as session:
        article = await session.get(NewsArticle, article_id)

        # 冪等性
        if await analysis_exists(session, article_id):
            return

        # API に送る前にトリミング（DB は変えない）
        text = article.original_content[:settings.max_analysis_input]

        try:
            result = await call_gemini_api(text)
            await save_analysis(article.id, result)
            await generate_embedding.kiq(article_id)

        except PermanentAPIError:      # Safety Block 等
            # 分析不可能な記事 → 本文を消して skip 扱い
            article.original_content = None
            article.skip_content_fetch = True
            await session.commit()

        except TemporaryAPIError:      # 429, 5xx, タイムアウト
            raise  # ブローカーにリトライさせる
```

Safety Block が起きた場合、original_content を NULL に戻し skip_content_fetch = True にすることで、
既存の仕組みで処理対象から外れる。追加カラム不要。

### generate_embedding のエラーハンドリング

```python
@broker.task(retry_on_error=True, max_retries=3)
async def generate_embedding(article_id: int, context: Context = TaskiqDepends()):
    async with get_session() as session:
        article = await session.get(NewsArticle, article_id)

        # 冪等性
        if await embedding_exists(session, article_id):
            return

        text = article.original_content[:settings.max_embedding_input]

        try:
            vector = await call_embedding_api(text)
            await save_embedding(article.id, vector)

        except TemporaryAPIError:
            raise  # ブローカーにリトライさせる
```

埋め込み生成で永続的失敗が起きるケースは現時点では想定しにくい。
将来必要になったら Safety Block と同じパターンで対処可能。

---

## 最終リトライの検知

SimpleRetryMiddleware は `message.labels["_retries"]` にリトライ回数を蓄積する（ソースコード確認済み）。

```python
def _is_last_attempt(context: Context) -> bool:
    retries = int(context.message.labels.get("_retries", 0))
    max_retries = int(context.message.labels.get("max_retries", 3))
    return (retries + 1) >= max_retries
```

fetch_content の一時的失敗で使用。最終リトライでも失敗した場合に skip_content_fetch = True を設定する。

---

## 冪等性の保証

RedisStreamBroker + Ack の設計では、稀にタスクが重複実行される可能性がある
（Ack 送信直前のクラッシュ → xautoclaim で再配達 → 2回目の実行）。
各タスクは処理済みチェックを先頭で行い、重複実行を安全に無視する:

| タスク | 冪等性チェック |
|---|---|
| fetch_content | `if article.original_content is not None: return` |
| analyze_article | `if await analysis_exists(session, article_id): return` |
| generate_embedding | `if await embedding_exists(session, article_id): return` |

---

## メソッド一覧

| メソッド | 種類 | ファイル | 役割 |
|---|---|---|---|
| `_is_last_attempt` | ヘルパー | pipeline_helpers.py | 最終リトライ判定 |
| `fetch_metadata` | タスク | pipeline_tasks.py | メタデータ取得 |
| `dispatch_pending` | タスク | pipeline_tasks.py | 未処理記事の振り分け |
| `fetch_content` | タスク | pipeline_tasks.py | 本文取得 + 品質ゲート |
| `analyze_article` | タスク | pipeline_tasks.py | AI分析 |
| `generate_embedding` | タスク | pipeline_tasks.py | 埋め込み生成 |

v2 から削除されたもの:
- `track_pipeline_status` → DB status を管理しないため不要
- `recover_interrupted` → RedisStreamBroker の xautoclaim が代替

---

## 可観測性

パイプラインの進捗は Redis 側で確認する:

```bash
# Stream 内のメッセージ総数（処理済み含む）
redis-cli XLEN taskiq

# Consumer Group の Pending メッセージ数（未 Ack = 処理中 or クラッシュ）
redis-cli XPENDING taskiq taskiq - + 10

# Consumer ごとの Pending 数
redis-cli XINFO GROUPS taskiq
```

注: 全タスクは単一の Stream (`taskiq`) に入る。タスク種別ごとの分離は queue_name を変えることで可能だが、現時点では不要。

DB 側の集計（管理者向け）:

```sql
-- 全体の進捗
SELECT
  COUNT(*) FILTER (WHERE original_content IS NULL AND skip_content_fetch = FALSE) as pending_fetch,
  COUNT(*) FILTER (WHERE skip_content_fetch = TRUE) as skipped,
  COUNT(*) FILTER (WHERE original_content IS NOT NULL) as has_content
FROM news_articles;

-- 分析の進捗
SELECT
  COUNT(*) FILTER (WHERE aa.id IS NULL) as pending_analysis,
  COUNT(*) FILTER (WHERE aa.id IS NOT NULL) as analyzed
FROM news_articles na
LEFT JOIN article_analyses aa ON aa.news_article_id = na.id
WHERE na.original_content IS NOT NULL;
```

---

## 影響範囲（コードベース調査済み）

### 変更が必要なファイル

| ファイル | 影響度 | 変更内容 |
|---|---|---|
| `app/tasks/taskiq_worker.py` | CRITICAL | ブローカー変更 + モノリシックタスクを6タスクに分割。実質的に `pipeline_tasks.py` に書き直し |
| `app/models/news.py` | HIGH | `skip_content_fetch` 追加、レガシー3カラム除去（マイグレーション B） |
| `app/services/content_extractor.py` | HIGH | レガシーカラムへの書き込み4箇所を除去（L241, L256, L257, L265）。エラー処理を raise 方式に変更 |
| `app/routers/news.py` | HIGH | `fetch_and_analyze_task` → 新タスクへの呼び出し変更 (L27, L346) |
| `tests/test_taskiq_worker.py` | HIGH | 全面書き直し（モノリシックタスク前提のテスト6本） |
| `tests/test_content_extractor.py` | MODERATE | `content_fetch_attempts` アサーション修正 (L427) |
| `tests/test_routers/test_news.py` | MODERATE | POST /fetch のモック対象変更 |
| `docker-compose.yml` | LOW | worker コマンドに `--ack-type when_executed` 追加 |

### 新規作成するファイル

| ファイル | 役割 |
|---|---|
| `app/tasks/pipeline_tasks.py` | 6タスク関数 + ブローカー + スケジューラー定義 |
| `app/tasks/pipeline_helpers.py` | `_is_last_attempt` 等のヘルパー |
| Alembic マイグレーション A | `skip_content_fetch` カラム + インデックス追加 |
| Alembic マイグレーション B | レガシー3カラム DROP（動作確認後） |

### 変更不要なファイル

| ファイル | 理由 |
|---|---|
| `app/services/ai_analyzer.py` | `original_content` のみ参照。レガシーカラム依存なし |
| `app/services/embedding.py` | 同上。`_build_embed_text()` は `original_content` を使用 |
| `app/services/dedup.py` | `ArticleAnalysis.embedding` のみ依存 |
| `app/services/news_fetcher.py` | `original_content` に書き込み。レガシーカラム不使用 |
| `app/services/hacker_news.py` | メタデータのみ保存。本文取得は関与しない |
| `app/services/alpha_vantage.py` | 同上 |
| `app/utils/redis_cache.py` | HTTP キャッシュ専用。ブローカーの Redis Stream とは別空間 |
| `app/schemas/news.py` | API レスポンス形式に変更なし |
| `tests/test_ai_analyzer.py` | レガシーカラム参照なし |
| `tests/test_news_fetcher.py` | レガシーカラム参照なし |
| `tests/conftest.py` | フィクスチャ定義にレガシーカラム直接参照なし |

### レガシーカラム参照の全箇所（除去対象）

| カラム | ファイル | 行 | 用途 |
|---|---|---|---|
| `content_fetched_at` | `taskiq_worker.py` | 219 | 未処理記事の SELECT 条件 |
| `content_fetched_at` | `content_extractor.py` | 257, 265 | 成功/失敗時のタイムスタンプ |
| `content_fetch_attempts` | `taskiq_worker.py` | 220-221 | リトライ上限チェック |
| `content_fetch_attempts` | `content_extractor.py` | 241 | エラー時カウント増加 |
| `content` | `content_extractor.py` | 256 | レガシー列への複製書き込み |

---

## Alembic マイグレーション

### マイグレーション A: 新カラム追加 + ブローカー変更

```sql
ALTER TABLE news_articles ADD COLUMN skip_content_fetch BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX idx_content_fetch_pending
    ON news_articles (skip_content_fetch)
    WHERE original_content IS NULL AND skip_content_fetch = FALSE;
```

同時に:
- ブローカーを ListQueueBroker → RedisStreamBroker に変更
- パイプラインコード（pipeline_tasks.py, pipeline_helpers.py）をデプロイ
- docker-compose.yml に `--ack-type when_executed` 追加

→ 動作確認

### マイグレーション B: 旧カラム DROP（動作確認後）

```sql
ALTER TABLE news_articles DROP COLUMN content;
ALTER TABLE news_articles DROP COLUMN content_fetched_at;
ALTER TABLE news_articles DROP COLUMN content_fetch_attempts;
```

downgrade: `raise NotImplementedError("Irreversible migration")`

同時に:
- `app/models/news.py` からレガシーカラム定義を除去
- `content_extractor.py` からレガシーカラムへの書き込みを除去

---

## 実装順序

```
Step 1: Alembic マイグレーション A — skip_content_fetch カラム + インデックス追加
Step 2: pipeline_tasks.py + pipeline_helpers.py 新規作成
Step 3: ブローカー変更（RedisStreamBroker）+ docker-compose.yml 更新
Step 4: content_extractor.py からレガシー書き込み除去
Step 5: routers/news.py の呼び出し先変更
Step 6: テスト改修
Step 7: 動作確認
Step 8: Alembic マイグレーション B — レガシーカラム DROP + モデル定義除去
```

---

## v2 → v3 → v4 の主な変更点

| 項目 | v2 | v3 | v4 (検証後) |
|---|---|---|---|
| DB の状態カラム | 3カラム × 5段階 | skip_content_fetch (bool) 1本 | 同左 |
| ブローカー | ListQueueBroker | RedisStreamBroker (Ack 付き) | 同左（互換性検証済み） |
| クラッシュ復旧 | startup 時に DB リセット | ブローカーの Ack が自動再配達 | xautoclaim による自動回収（10分閾値、排他ロック付き） |
| Ack タイプ | — | 未指定 | `--ack-type when_executed`（Ack→リトライ投入の順序を保証） |
| 状態遷移管理 | track_pipeline_status | 不要 | 同左 |
| 永続的失敗の記録 | status = 'failed' | skip_content_fetch = True | 同左 |
| 可観測性 | DB の status カラム | Redis XLEN + DB 集計 | Redis XPENDING/XINFO + DB 集計 |
| 複数ワーカー対応 | 未対応 | Ack で自動対応 | xautoclaim の排他ロックで安全に対応 |
| Stream メモリ管理 | — | 未指定 | maxlen=10,000 + approximate=True |
| 影響範囲 | 未調査 | 未調査 | 全ファイル調査済み（上記テーブル参照） |

---

## 保留タスク

- C-3 カラム除去: 新パイプラインが完全に動作確認できてから実施（マイグレーション B）
- compare_models.py の修正: C-3 のスコープに含まれる
- コンテンツ抽出の品質チューニング: trafilatura の設定見直し（パイプライン刷新とは別タスク）
