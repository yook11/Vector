# Stage 2 (content_fetch) 監査統合 + transient 救済 設計メモ

PR2 / PR2.5 の設計討議メモ (2026-05-05)。実装着手前に固める。

ADR: `docs/observability/pipeline-events-design.md`
ロードマップ: memory `project_pipeline_events_pr_roadmap.md`

---

## 背景

PR1 で `pipeline_events` 監査基盤 + Stage 1 (`source_fetch`) 統合済。
PR1.5 で Fetcher 型階層整理 + metadata observation 活性化 済。

Stage 2 (= Pattern H 2 段目 = 現 `extract_html_body` task) は監査ゼロ。
PR2 で Stage 2 にも audit を入れる。

その過程で **「再試行すべきか否かの区別が現状無い」** ことが判明
(404 で死んだ URL も毎 cron 再試行されている)。
→ PR2.5 で抑止 + transient 救済まで含めて解消する。

---

## 失敗モード分類 (PR2 設計の基盤)

| # | 失敗 | 取りたい行動 | retry 価値 (次 cron) |
|---|---|---|---|
| 1 | HTTP 5xx / timeout / DNS | taskiq retry | あり (サーバ復活で成功しうる) |
| 2 | HTTP 403/404/410/451/SSRF blocked | 即 drop | **無し** (URL dead) |
| 3 | HTML 取れたが trafilatura で empty | 即 drop | **無し** (サイト構造) |
| 4 | promotion `Failed` (body_too_short / pubdate_missing 等) | 即 drop | **無し** (本文短いまま / pubdate 無いまま) |
| 5 | DB race-lost (save→None, find→既存) | 既存で先へ進む | 失敗ではない |

「無し」5 行が **PR2.5 で skip 対象**。

---

## PR2: ContentFetchService の切り出し + audit 統合

### 決定 #1: Outcome shape

```python
@dataclass(frozen=True, slots=True)
class ContentFetched:
    article: Article  # race-lost 時の既存検出も含む

@dataclass(frozen=True, slots=True)
class TerminallyDropped:
    """二度試しても無意味 (URL dead / content unusable)。"""
    reason_code: str

@dataclass(frozen=True, slots=True)
class TransientlyDropped:
    """次 cron で再試行する価値あり (TemporaryFetchError exhausted のみ)。"""
    reason_code: str

ContentFetchOutcome = ContentFetched | TerminallyDropped | TransientlyDropped
```

理由: terminal vs transient の **3 状態** が後段の skip / 救済判断に必要。
nullable では表現できない (third state 蓋然性あり) → discriminated union。

### 決定 #2: エラーハンドリングのチャネル分け

**チャネル 1 (例外): retry 判断が要るもの**
- `TemporaryFetchError` のみ Service から raise → task が `is_last_attempt`
  で判断 (taskiq retry policy は task の責務)。

**チャネル 2 (戻り値): Service が完結処理したもの**
- `PermanentFetchError` → catch → audit + `TerminallyDropped` 返却
- `ExtractionEmpty` → audit + `TerminallyDropped`
- promotion `Failed` → audit + `TerminallyDropped(reason_code="promotion_xxx")`
- race-lost → 既存 Article 検出 → `ContentFetched`
- 成功 → audit + `ContentFetched`

```
┌─────────────────────────────────────────────┐
│ ContentFetchService.execute(staged, attempt)│
│                                             │
│   ┌─ HTTP 取得                              │
│   │   PermanentFetchError → audit + Terminal│
│   │   TemporaryFetchError → raise (素通し)  │ ──→ task が retry 判断
│   │                                         │
│   ├─ ExtractionEmpty   → audit + Terminal   │
│   ├─ promotion Failed  → audit + Terminal   │
│   ├─ race-lost         → ContentFetched(既存)│
│   └─ 成功              → audit + Fetched    │
└─────────────────────────────────────────────┘

┌────────────────────────────────────┐
│ task 側                            │
│  try:                              │
│    outcome = svc.execute(...)      │
│  except TemporaryFetchError:       │
│    if is_last_attempt(ctx):        │
│      svc.audit_exhausted(...)      │ ← 別 method
│      return None                   │
│    raise                           │
│                                    │
│  match outcome:                    │
│    case ContentFetched(article):   │
│      ready = ReadyForExtraction... │
│      await extract_content.kiq(...)│
│    case TerminallyDropped():       │
│    case TransientlyDropped():      │
│      pass  # do nothing            │
└────────────────────────────────────┘
```

### 決定 #3: ContentFetchPayload (`pipeline_events.payload`)

既存 skeleton (`backend/app/observability/domain/payloads.py:57-68`) を活性化 + 微調整。

| field | PR2 で使うか | 用途 |
|---|---|---|
| `discovered_article_id: int` | ✓ | PR2.5 の skip 判定 key |
| `extractor_class: str` | ✓ | `ArticleHtmlExtractor` 等を焼く |
| `http_status: int \| None` | ✓ | 403/404 等を SQL 集計可能に |
| `final_url` / `response_size` / `content_type` / `body_head` | ✓ (失敗時のみ) | S 級失敗診断 snapshot |
| `quality_gate_metric: dict \| None` | ✓ | promotion Failed の `{"body_length": 23}` 等 |
| `body_length: int \| None` | **新規** | 成功時の本文長分布観測 (独立フィールド) |
| `reason_code: str \| None` | **新規** | drop 詳細 (`permanent_fetch_error` / `extraction_empty_quality_gate` / `promotion_body_too_short` 等) |
| `published_at_source` | ✗ | YAGNI (将来必要なら追加) |

`outcome_code` (共通カラム) の値:
- `"fetched"` (ContentFetched)
- `"dropped_terminal"` (TerminallyDropped)
- `"dropped_transient"` (TransientlyDropped)

### 決定 #4: kiq 起動位置

**task 側で `extract_content.kiq` を打つ** (Phase 4 入口 task pattern と同じ)。
Service は kiq 副作用を持たない (Service テストが broker mock 不要)。

### 決定 #5: `is_last_attempt` の責務

**task が判断**。Service は taskiq retry 概念を知らない。

- task が `TemporaryFetchError` を catch → `is_last_attempt(ctx)` 判定
- last attempt なら `svc.audit_exhausted(staged, attempt)` 呼出 → return None
- 通常時は raise (taskiq broker が retry)

→ Service の `execute` シグネチャは `is_last_attempt` を取らない。
別 method `audit_exhausted` を Service に持たせる。

---

## PR2.5: terminal 抑止 + transient 救済

### 問題提起

PR2 まででも:
- 404/410 で死んだ URL を **毎 cron 再試行している** (RSS feed window が抱えてる限り)
- Transient 失敗で taskiq budget を使い切った後、URL が RSS window から落ちると **データ消失**
  (確率は低いが非ゼロ)

`pipeline_events` は監査ログであって state store ではない (履歴 vs 状態の分離)。
runtime 判断には別の state を用意する必要がある。

### `discovered_articles` の現状

```python
# backend/app/models/discovered_article.py
class DiscoveredArticle:
    id, news_source_id, original_url, original_title, discovered_at
```

`PendingHtmlFetch` 再構築に必要なフィールド:

| field | discovered_articles 由来 | 状態 |
|---|---|---|
| `title` | `original_title` | ✓ |
| `source_id` | `news_source_id` | ✓ |
| `source_url` | `original_url` | ✓ |
| `published_at_hint` | **どこにも無い** | ✗ |
| `prefer_html_title` | Fetcher class 配下 | ✓ (config 復元) |

**`published_at_hint` が永続化されていない** のが救済時の弱点。
無いまま再試行すると `try_advance_from` の merge で HTML フォールバックに依存。
HTML 側に pubdate が無いソースは `Failed(published_at_missing)` で TerminallyDropped 化する。

### PR2.5 で行う変更

#### alembic: 列を 2 本追加

```sql
ALTER TABLE discovered_articles
  ADD COLUMN terminal_drop_reason TEXT NULL,
  ADD COLUMN published_at_hint TIMESTAMPTZ NULL;
```

- `terminal_drop_reason`: TerminallyDropped 時に Service が同 tx で update。Stage 1 が skip 判定。
- `published_at_hint`: Stage 1 が Pattern H entry 投入時に populate。recovery cron が再構築時に使用。

Pattern R 行は両カラム NULL のまま (Pattern R は再試行不要)。

#### コード変更

1. **ContentFetchService**: TerminallyDropped 時に
   - `pipeline_events` に焼く (PR2 と同じ)
   - **同 tx で `discovered_articles.terminal_drop_reason` を update** (PR2.5 で追加)
2. **IngestionService (`_upsert_discovered_url`)**:
   - Pattern H 時は `published_at_hint` を populate
   - `terminal_drop_reason IS NOT NULL` の URL は staged_list に入れない
3. **Recovery cron task** (新規):
   - 別 cron schedule (例: 30 min ごと、RSS cron より長め)
   - クエリ:
     ```sql
     SELECT d.id, d.original_url, d.original_title,
            d.news_source_id, d.published_at_hint
     FROM discovered_articles d
     LEFT JOIN articles a ON a.discovered_article_id = d.id
     WHERE a.id IS NULL
       AND d.terminal_drop_reason IS NULL
       AND d.discovered_at < NOW() - INTERVAL '30 minutes'  -- active task との race 回避
     LIMIT 100  -- back-pressure
     ```
   - `PendingHtmlFetch` を再構築 (`prefer_html_title` は Fetcher class から復元)
   - `extract_html_body.kiq(staged)` で再投入
4. **テスト**:
   - terminal skip 動作 (`_upsert_discovered_url` が `terminal_drop_reason IS NOT NULL` を skip)
   - recovery cron が正しい行を select する
   - reconstruction で `published_at_hint=None` でも HTML 側に pubdate あれば成功する

### スコープ分割の根拠

PR2 と PR2.5 を分ける理由:
- PR2 = **観測の整備** (audit 焼き付け、行動は変えない)
- PR2.5 = **問題解消** (skip + 救済、行動を変える、列追加 alembic 必要)

PR2 が観測を整えてから PR2.5 が行動を変えるので、PR2.5 で問題が出ても PR2 の audit を見て診断できる。

---

## 実装順序

PR2 → PR2.5 の **連続実装** (PR2 merge 後すぐ PR2.5 着手)。

### PR2 の手順
1. `ContentFetchService` 切り出し (新規ファイル)
2. `ContentFetchOutcome` discriminated union 定義
3. `extract_html_body` task を Service 経由に書き換え + kiq は task 側維持
4. `ContentFetchPayload` 微調整 (`reason_code`, `body_length` 追加)
5. `outcome_code` 3 値 (`fetched` / `dropped_terminal` / `dropped_transient`)
6. テスト: Outcome 各 variant / payload 観測 / `is_last_attempt` 経路
7. ruff + pytest + 1 ソース dispatch で `pipeline_events` 確認

### PR2.5 の手順
1. alembic: `terminal_drop_reason` + `published_at_hint` 列追加
2. ContentFetchService: TerminallyDropped 時に同 tx で `terminal_drop_reason` update
3. IngestionService: `published_at_hint` populate + skip 判定
4. Recovery cron task 新設
5. テスト: skip 動作 / recovery 経路 / reconstruction
6. 1 ソースで terminal drop → 次 cron で skip されることを確認

---

## 持ち越し論点

- **PR2.5 で recovery cron の頻度** (30 min / 1 hour / 等) は実装時に決める
- **`published_at_hint` の値域**: Pattern R 行も RSS pubdate を入れて将来活用するか? 今は NULL 維持を推奨 (Pattern R は articles.published_at が真の値、hint は冗長)
- **`pipeline_events` の retention**: 監査ログを古いものから消す運用は別 PR で
