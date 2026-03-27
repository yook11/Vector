# Step 2: タスク分割 — モノリシック → チェーンタスク

親ドキュメント: [pipeline_architecture.md](../pipeline_architecture.md)

前提: [Step 1](step1-broker-migration.md) 完了（RedisStreamBroker 動作確認済み）

## 目的

`fetch_and_analyze_task`（1つの巨大バッチタスク）を、記事単位の独立タスクに分割する。
障害分離・個別リトライ・チェーン実行を実現する。

> **スコープ外: 意味的重複検出（detect_duplicates）**
> 現行パイプラインの Phase 6（embedding 間コサイン距離によるグルーピング）は
> Step 2 に含めない。グルーピングは per-article チェーンとは直交する
> cross-article バッ　チ操作であり、DB スキーマ（グループテーブル）・API・UI と
> 合わせて別 Step で設計する。
> Collection 層（URL 重複チェック）は fetch_metadata 内で従来通り動作する。
> 詳細は [future-considerations.md](future-considerations.md) を参照。

---

## 新しいアーキテクチャ

```
scheduler (30分ごと)
  → fetch_metadata           — バッチで RSS/HN 取得
    → dispatch_pending       — 未処理記事を SELECT → 個別タスクとしてエンキュー
      → fetch_content(id)    — 個別タスク、Redis がリトライ管理
        → analyze_article(id)  — 成功時にチェーン
          → generate_embedding(id) — 成功時にチェーン
```

### 2層リトライモデル

- **ミクロ**（ack + SimpleRetryMiddleware）: 秒〜分単位。ブローカーが管理
- **マクロ**（dispatch_pending 30分ごと）: 障害復旧・バックフィル。DB走査で未完了記事を再投入

---

## 新規作成するファイル

### `backend/app/tasks/pipeline_tasks.py`

ブローカー・スケジューラー定義 + 5つのタスク関数 + `_is_last_attempt` ヘルパー:

| タスク | timeout | max_retries | トリガー | 責務 |
|---|---|---|---|---|
| `fetch_metadata` | 300s | 2 | cron (30分) | RSS/HN からメタデータ取得 → `dispatch_pending.kiq()` |
| `dispatch_pending` | 60s | 1 | `fetch_metadata` 完了時のみ（独立 cron なし） | 未処理記事を SELECT → 個別タスクとしてエンキュー (LIMIT 100) |
| `fetch_content(id)` | 90s | 3 | `dispatch_pending` | 本文取得 + 品質ゲート → 成功時 `analyze_article.kiq(id)` |
| `analyze_article(id)` | 180s | 2 | `fetch_content` 成功時 or `dispatch_pending` | AI 分析 → 成功時 `generate_embedding.kiq(id)` |
| `generate_embedding(id)` | 60s | 2 | `analyze_article` 成功時 or `dispatch_pending` | 埋め込み生成 |

`_is_last_attempt(ctx)`: SimpleRetryMiddleware の最終リトライを検知するプライベート関数。

---

## 変更するファイル

### 1. `backend/app/tasks/taskiq_worker.py` → 廃止

`pipeline_tasks.py` に全機能を移行。旧ファイルは削除する。

**依存グラフ（import 元）:**
- `backend/app/routers/news.py`: `from app.tasks.taskiq_worker import fetch_and_analyze_task`
- `backend/tests/test_taskiq_worker.py`: 同上（テストファイル → リネーム対象）
- `docker-compose.yml`: worker/scheduler のモジュールパス

上記以外に `taskiq_worker` を参照しているファイルはない。

### 2. `backend/app/models/news.py` — カラム追加

```python
skip_content_fetch: bool = Field(default=False)
```

Alembic マイグレーション:
```sql
ALTER TABLE news_articles ADD COLUMN skip_content_fetch BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX idx_content_fetch_pending
    ON news_articles (skip_content_fetch)
    WHERE original_content IS NULL AND skip_content_fetch = FALSE;
```

### 3. `backend/app/services/content_extractor.py` — エラー分類の導入

`extract_content()`（単一記事関数）にエラー分類を導入:
- `PermanentFetchError`: 403, 404, robots.txt 拒否 → タスク側で `skip_content_fetch = True`
- `TemporaryFetchError`: 5xx, タイムアウト, 429 → タスク側で raise してリトライ委譲
- `None` 返却: パース失敗・品質不足 → タスク側で `skip_content_fetch = True`
- `str` 返却: 正常

`extract_contents()`（バッチ関数）: 呼び出し元は taskiq_worker.py のみ。廃止で自然に不要になるが、
**Step 2 では削除せず残す**。理由: 検証中に問題が出た場合に旧パイプラインへのロールバックを容易にするため。
`# TODO: Step 2 完了後に削除候補（呼び出し元なし）` コメントを付与する。
`_fetch_one` の try/except で例外 raise 化との互換性は維持。

レガシーカラム（`content`, `content_fetched_at`, `content_fetch_attempts`）への**書き込みのみ除去**。
カラム DROP は Phase 7 C-3 のスコープ。書き込みを止めても読み込み側が壊れないことの確認:
- `content_fetched_at` / `content_fetch_attempts`: 読み取りは taskiq_worker.py（廃止）のみ → 安全
- `content`: 読み取りは compare_models.py のみ → **Step 2 スコープ外**（下記参照）

### 4. `backend/app/routers/news.py` — タスク呼び出し変更

```python
# 変更前
from app.tasks.taskiq_worker import fetch_and_analyze_task
task = await fetch_and_analyze_task.kiq(source_ids=source_ids)

# 変更後
from app.tasks.pipeline_tasks import fetch_metadata
task = await fetch_metadata.kiq(source_ids=source_ids)
```

> `source_ids` は `fetch_metadata` でのソース取得フィルタとしてのみ使用される。
> `dispatch_pending` には伝搬せず、全未処理記事を対象にする（現行動作と同じ）。

### 5. `docker-compose.yml` — モジュールパス更新

```yaml
# worker
command: >
  taskiq worker
  app.tasks.pipeline_tasks:broker
  app.tasks.pipeline_tasks
  --ack-type when_executed

# scheduler
command: taskiq scheduler app.tasks.pipeline_tasks:scheduler
```

### ~~6. `backend/app/scripts/compare_models.py` — レガシーカラム参照修正~~ → **スコープ外**

compare_models.py には `article.content` の他にも Phase 7c-1 リネーム追随漏れが4箇所ある
（`title_original`, `analyses`, `fetched_at`, `description_original`）。
これらは Step 2 の意図（タスク分割）とは無関係であり、PR に混ぜると変更の意図が曖昧になる。
**Phase 7 C-3 タスクに紐づけて別途対応する。**

---

## fetch_content タスクの詳細設計

httpx.AsyncClient / RobotsCache はタスクごとに都度生成（1タスク1HTTPリクエスト）。

```python
async def fetch_content(article_id, ctx):
    article = session.get(NewsArticle, article_id)
    if article.original_content is not None:
        return  # 冪等性ガード

    try:
        content = await extract_content(client, article.original_url, robots_cache)
    except PermanentFetchError as e:
        article.skip_content_fetch = True
        logger.info("fetch_content_skip", article_id=article_id, reason=str(e))
        return
    except TemporaryFetchError:
        if _is_last_attempt(ctx):
            article.skip_content_fetch = True
            logger.warning("fetch_content_max_retries", article_id=article_id)
            return
        raise  # → SimpleRetryMiddleware がリトライ

    if content is None:  # 品質ゲート不通過
        article.skip_content_fetch = True
        logger.info("fetch_content_skip", article_id=article_id, reason="quality_gate")
        return

    article.original_content = content
    await analyze_article.kiq(article_id)
```

---

## 冪等性の設計

| タスク | チェック | 理由 |
|---|---|---|
| `fetch_content` | `if article.original_content is not None: return` | xautoclaim による重複配達 |
| `analyze_article` | `if await analysis_exists(session, article_id): return` | 同上 |
| `generate_embedding` | `if await embedding_exists(session, article_id): return` | 同上 |

---

## dispatch_pending の判定ロジック

3クエリで未処理記事を走査。各 LIMIT 100 で大量 kiq を防止。
正常チェーンとの二重投入は冪等性チェックで安全に弾く。

```sql
-- 本文取得
SELECT id FROM news_articles
WHERE original_content IS NULL AND skip_content_fetch = FALSE
LIMIT 100;

-- AI分析（障害復旧・バックフィル用）
SELECT na.id FROM news_articles na
LEFT JOIN article_analyses aa ON aa.news_article_id = na.id
WHERE na.original_content IS NOT NULL AND aa.id IS NULL
LIMIT 100;

-- 埋め込み（障害復旧・バックフィル用）
SELECT na.id FROM news_articles na
JOIN article_analyses aa ON aa.news_article_id = na.id
WHERE aa.embedding IS NULL
LIMIT 100;
```

無限ループが起きない理由:
- `fetch_content` の品質ゲートが後続タスクの入力品質を保証
- Query 1: `skip_content_fetch` で永久失敗を除外
- Query 2: `original_content IS NOT NULL` = 品質保証済み。
  Gemini Safety Block 時は `original_content = None` + `skip_content_fetch = True` で全クエリから除外
- Query 3: 分析済みデータが入力

---

## エラーハンドリング

詳細は親ドキュメントの「エラーハンドリング戦略」セクションを参照。

### fetch_content のエラー処理
- 永続的失敗 → `skip_content_fetch = True`、raise しない
- 一時的失敗 → raise してブローカーにリトライを委譲
- 最終リトライ失敗 → `_is_last_attempt()` で検知し `skip_content_fetch = True` を書いて return。
  ブローカーから見ると「成功」扱い（raise しない = ack される）。これは意図的な動作:
  もうリトライさせたくないタスクを「成功終了」としてブローカーから消す。
- skip 理由は構造化ログ（structlog の reason フィールド）で記録。DBカラムは追加しない

### analyze_article のエラー処理
- Gemini Safety Block 等の永続エラー → `original_content = None` + `skip_content_fetch = True`
  コンテンツを無効化し再取得も防止。追加カラム不要で全 dispatch クエリから除外される:
  - Query 1: `skip_content_fetch = True` → 除外
  - Query 2: `original_content IS NULL` → 除外
- 一時的失敗（429, 5xx） → raise してリトライ委譲

---

## テスト方針

### サービス層テスト（既存を維持）
- `test_content_extractor.py`: PermanentFetchError/TemporaryFetchError のテストケースを追加
- 他のテストファイルはサービス関数のインターフェース不変のため影響なし

### タスク層テスト（`tests/test_taskiq_worker.py` → `tests/test_pipeline_tasks.py` にリネーム+書き換え）

既存テストはモノリシックタスクのモックパターンであり、新チェーンタスクには構造が合わない。
ただし以下のヘルパーは流用可能:
- `_mock_session_context()`: AsyncSession のコンテキストマネージャーモック
- `_make_ctx()`: taskiq Context モック（`ctx.state.engine` を持つ）

テストケース:
- 各タスクの冪等性ガード（既に処理済みの記事 → 即 return）
- チェーン発火（fetch_content 成功 → analyze_article.kiq 呼び出し）
- エラーハンドリング（PermanentFetchError → skip_content_fetch = True）
- dispatch_pending の3クエリ（本文未取得 / 未分析 / 未埋め込み）
- broker は taskiq の InMemoryBroker を使用

### 統合テスト
- Step 2 のスコープ外

---

## 検証項目

- [ ] `fetch_metadata` → `dispatch_pending` → `fetch_content` → `analyze_article` → `generate_embedding` のチェーンが動作すること
- [ ] 1件の `fetch_content` 失敗が他の記事に影響しないこと
- [ ] `skip_content_fetch = True` の記事が再 dispatch されないこと
- [ ] 冪等性: 同じ article_id で2回実行しても副作用がないこと
- [ ] `POST /api/v1/news/fetch` が新タスク構造で動作すること
- [ ] dedup（`detect_duplicates`）が呼ばれないこと（意図的除外の確認）
- [ ] `ruff check` + `pytest` が通ること
