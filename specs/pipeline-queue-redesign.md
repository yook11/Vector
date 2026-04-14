# パイプラインキュー再設計 — 問題分析

> 作成日: 2026-04-14

## 現状の構造

```
[cron] → fetch_metadata → dispatch_pending ─→ fetch_content
                                            ─→ analyze_article
                                            ─→ generate_embedding

[admin] → PipelineService.backfill_embeddings → generate_embedding
        → PipelineService.submit_fetch        → fetch_metadata
```

- 全タスクが単一の `RedisStreamBroker` に混在（`tasks/pipeline_tasks.py`）
- タスク完了後のチェーン遷移あり（`fetch_content` → `analyze_article` → `generate_embedding`）
- `dispatch_pending` が全状態を一括スキャンして振り分ける中間タスクとして存在

## 問題 1: 判定ロジックの散在

「この記事は埋め込みが必要か？」の判定が3箇所に存在する。

| 場所 | 判定方法 | 用途 |
|------|---------|------|
| `dispatch_pending` (L248-264) | インライン SQL | cron 経由の通常フロー |
| `PipelineRepository` (L14-25) | Repository メソッド | admin バックフィル |
| `generate_embedding` (L424) | 冪等性ガード | タスク実行時の安全装置 |

1 と 2 は同じ WHERE 条件の重複。片方だけ修正してもう片方を忘れるリスクがある。
3 は分散システムの安全装置であり、別の責務。

同じ構造がコンテンツ取得・AI分析にも存在する:

| 判定 | dispatch_pending (inline SQL) | Repository | タスク内ガード |
|------|------------------------------|------------|--------------|
| コンテンツ未取得 | L209-223 | なし | L290-291 |
| 分析未完了 | L226-245 | なし | L346-354 |
| 埋め込み未完了 | L248-264 | L14-25 | L424 |

## 問題 2: `dispatch_pending` の不自然さ

`dispatch_pending` は「全状態を一括スキャンして、各タスクキューに振り分ける」関数。
しかし、既にチェーン遷移が存在する:

- `fetch_content` 完了 → `analyze_article.kiq()` (L328)
- `analyze_article` 完了 → `generate_embedding.kiq()` (L394)

つまり通常フローではチェーンで次に進む。`dispatch_pending` は「チェーンから漏れた記事を拾う」補助的な役割でしかない。
にもかかわらず、3種の判定ロジックをインラインで持ち、タスクファイルの複雑さの主因になっている。

## 問題 3: 全タスクが単一キューに混在

5つのタスク（`fetch_metadata`, `dispatch_pending`, `fetch_content`, `analyze_article`, `generate_embedding`）が
1つの `RedisStreamBroker` を共有している。

これにより:
- 各タスク種別ごとのワーカー並列度を制御できない（埋め込みは並列1、取得は並列10、のような調整が不可）
- 障害が波及する（埋め込み API が落ちてリトライが溜まると、取得・分析のワーカーリソースを圧迫）
- キュー単位での観測ができない（どのタスクが詰まっているか見えにくい）

## 問題 4: `PipelineService` は実体のないラベル

`PipelineService` は `backfill_embeddings` と `submit_fetch` の2メソッドを持つが:

- この2つが同居する理由は「どちらもパイプラインの一部だから」
- 「パイプライン」はドメイン概念ではなく、一連の処理を指すラベル
- 各メソッドは 7 行程度の薄いラッパーで、独立した責務

## 問題 5: 投入経路が複数ある

`generate_embedding.kiq()` を呼ぶ経路が3つある:

| 経路 | 場所 |
|------|------|
| `dispatch_pending` | `tasks/pipeline_tasks.py` L263 |
| `PipelineService.backfill_embeddings` | `services/pipeline.py` L19 |
| `analyze_article` タスク末尾 | `tasks/pipeline_tasks.py` L394 |

各経路が独自の判断で直接 `kiq()` を呼んでおり、投入の窓口が一元化されていない。

## 根本原因

上記の問題は独立しているように見えるが、根本原因は1つ:

**「記事の処理状態の判定」と「次の処理への投入」が、明確な所有者を持たない。**

結果として、判定は複数箇所に散在し、投入経路も複数存在し、それらを束ねるために `dispatch_pending` という不自然な中間層が必要になっている。

---

## 実装プラン

### 設計方針

- キューを責務ごとに分離する。各キューには1種類のタスクだけが入る
- 処理が完了したら次のキューに入れる。それだけ
- `dispatch_pending`（一括スキャン振り分け）は廃止し、チェーン遷移に一本化する

### 目標構造

```
[fetch_queue]      fetch_metadata → fetch_content
                                         ↓ 完了したら次のキューへ
[analysis_queue]                   analyze_article
                                         ↓ 完了したら次のキューへ
[embedding_queue]                  generate_embedding
```

### taskiq での実現方法

taskiq の `RedisStreamBroker` はインスタンスごとに独立した Redis Stream を持つ。
broker を3つ作り、タスクを分離する。ワーカーは broker ごとに起動する。

```python
# brokers.py
broker_fetch     = RedisStreamBroker(url=..., queue_name="fetch")
broker_analysis  = RedisStreamBroker(url=..., queue_name="analysis")
broker_embedding = RedisStreamBroker(url=..., queue_name="embedding")
```

cross-broker のタスク呼び出し（例: fetch_content 完了 → analyze_article.kiq()）は、
対象タスクを import して `.kiq()` を呼ぶだけ。メッセージは対象タスクの broker に送られる。

---

### Step 1: キュー分離（ロジック変更なし）

**目的**: 既存のチェーン遷移を維持したまま、キューだけ分離する。
動作が変わらないことを検証できる最小の変更。

#### ファイル構成の変更

```
backend/app/tasks/
├── pipeline_tasks.py          # 現状: 全タスク + broker + scheduler (441行)
↓ 分割
├── brokers.py                 # broker 定義 × 3 + 共通 lifecycle hooks
├── fetch_tasks.py             # fetch_metadata, dispatch_pending, fetch_content
├── analysis_tasks.py          # analyze_article
├── embedding_tasks.py         # generate_embedding
```

#### brokers.py

- `broker_fetch`, `broker_analysis`, `broker_embedding` の 3 broker を定義
- 各 broker に同じ result backend + retry middleware を設定
- 各 broker に `WORKER_STARTUP`/`WORKER_SHUTDOWN` lifecycle hook を登録
  - 現状と同じ: engine の作成/破棄
- cron 設定と scheduler は `broker_fetch` にのみ紐づく（fetch_metadata だけが cron 対象）
- `_is_last_attempt` ヘルパーもここに配置

#### fetch_tasks.py

- `broker_fetch` を使用
- `fetch_metadata`: ロジック変更なし。末尾で `dispatch_pending.kiq()` を呼ぶ（現状維持）
- `dispatch_pending`: ロジック変更なし。3種のタスクを各 broker に投入
  - `fetch_content.kiq()` → broker_fetch
  - `analyze_article.kiq()` → broker_analysis（cross-broker import）
  - `generate_embedding.kiq()` → broker_embedding（cross-broker import）
- `fetch_content`: ロジック変更なし。末尾で `analyze_article.kiq()` を呼ぶ（cross-broker）

#### analysis_tasks.py

- `broker_analysis` を使用
- `analyze_article`: ロジック変更なし。末尾で `generate_embedding.kiq()` を呼ぶ（cross-broker）

#### embedding_tasks.py

- `broker_embedding` を使用
- `generate_embedding`: ロジック変更なし

#### docker-compose.yml の変更

```yaml
# 現状: worker × 1, scheduler × 1
# 変更後: worker × 3, scheduler × 1

worker-fetch:
  command: taskiq worker app.tasks.fetch_tasks:broker_fetch ...
worker-analysis:
  command: taskiq worker app.tasks.analysis_tasks:broker_analysis ...
worker-embedding:
  command: taskiq worker app.tasks.embedding_tasks:broker_embedding ...
scheduler:
  command: taskiq scheduler app.tasks.brokers:scheduler_fetch
```

#### admin ルーターの変更

- `POST /admin/pipeline/fetch`: `fetch_metadata.kiq()` を直接呼ぶ（import 先が変わるだけ）
- `POST /admin/pipeline/embed`: `PipelineService.backfill_embeddings` は一旦そのまま
  - import 先が `embedding_tasks.generate_embedding` に変わるだけ

#### 検証

- 既存の全フロー（cron → fetch → dispatch → 各タスク）が同じ動作をすること
- チェーン遷移が cross-broker で正しく動くこと
- admin エンドポイントが動作すること

---

### Step 2: dispatch_pending の廃止

**目的**: 一括スキャン振り分けを廃止し、チェーン遷移に一本化する。
判定ロジックの散在を解消する。

#### 変更内容

**2a. fetch_metadata が新規記事を直接 fetch_content キューに投入する**

現状の `fetch_metadata`:
```python
fr = await fetch_news_for_sources(session, sources)
await dispatch_pending.kiq()  # 一括スキャン
```

変更後:
```python
fr = await fetch_news_for_sources(session, sources)
for article_id in fr.new_article_ids:  # fetch 結果から新規記事 ID を取得
    await fetch_content.kiq(article_id)
```

→ `fetch_news_for_sources` の戻り値に `new_article_ids: list[int]` を追加する必要あり。

**2b. 冪等性ガードをチェーン遷移に修正**

現状: ガードに引っかかると `return` して終了（チェーンが途切れる）。

```python
# fetch_content
if article.original_content is not None:
    return  # チェーン切断
```

変更後: ガードに引っかかったら次のキューに送る。

```python
# fetch_content
if article.original_content is not None:
    await analyze_article.kiq(article_id)  # チェーン継続
    return
```

同様に `analyze_article`:
```python
if existing is not None:
    await generate_embedding.kiq(article_id)  # チェーン継続
    return
```

ただし `generate_embedding` の冪等性ガードはチェーンの末端なので `return` のまま。

**2c. dispatch_pending タスクを削除**

- `fetch_tasks.py` から `dispatch_pending` を削除
- `fetch_metadata` から `dispatch_pending.kiq()` の呼び出しを削除

**2d. PipelineService を解体**

- `submit_fetch`: admin ルーターから直接 `fetch_metadata.kiq()` を呼ぶ
- `backfill_embeddings`: → Step 3 で判断
- `services/pipeline.py` を削除

#### 検証

- cron → fetch_metadata → fetch_content → analyze_article → generate_embedding のチェーンが動作すること
- 既にコンテンツがある記事がチェーンをスキップせず次に進むこと
- admin の fetch エンドポイントが動作すること

---

### Step 3: バックフィルの扱いを決める

**目的**: `POST /admin/pipeline/embed`（埋め込みバックフィル）を残すか廃止するか判断する。

#### 判断材料

バックフィルが必要なケース（Step 2 完了後）:
1. タスクがリトライ上限で失敗 → チェーンが止まった記事
2. ワーカークラッシュ → チェーン遷移前にプロセスが死んだ
3. 日次クォータ到達 → return して終了、翌日の復旧手段がない

#### 選択肢

**A. バックフィルを残す**
- `PipelineRepository.get_article_ids_without_embedding()` を維持
- admin ルーターから直接 `generate_embedding.kiq()` を投入
- PipelineService は不要（ルーターで直接書ける程度の薄さ）

**B. バックフィルを廃止し、リカバリ cron に置き換える**
- 定期的に（例: 1日1回）チェーンの途切れた記事を検出して再投入
- admin の手動操作が不要になる
- ただし cron タスクの追加が必要

**C. バックフィルを廃止し、Dead Letter Queue で対応**
- 失敗したタスクを DLQ に入れ、必要に応じて再投入
- taskiq の機能として DLQ がどこまでサポートされているか要調査

→ Step 2 完了後に、実際に何がどの頻度で失敗するかを見て判断する。
