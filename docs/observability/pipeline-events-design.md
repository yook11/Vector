# Pipeline Events 監査基盤 設計 ADR

**Status**: Accepted
**Date**: 2026-05-03
**Owners**: yook11

---

## 背景

Vector のパイプラインは 4 broker × 5 Stage（dispatch / source_fetch / content_fetch / extraction / classification / embedding）と back-fill 3 系統で構成されている。現状の観測手段は以下に限られる：

- **structlog** の標準出力（コンテナ再起動で失われる、ローテーションで消える）
- `fetch_logs` テーブル（読み手ゼロ、stage 1 のみカバー）— **撤去済み (z7_drop_fetch_logs)**
- AI raw response は **どこにも残らない**（プロンプトインジェクション検知不能）
- 失敗の "事実" を SQL で集計する手段が無い

過去には Stage 2 が 17 時間停止していた事実をリアルタイムに検知できず、再現にも数時間を要した。本 ADR は **「3 ヶ月後の自分が、何が起きていたかを SQL 1 本で再構成できる」** ことを最小目標とした監査基盤を定義する。

---

## 決定

1. **監査を first-class 概念とし、`app/observability/` に bounded context を置く**
2. **`pipeline_events` 単一テーブル + Pydantic Discriminated Union payload** で 5 Stage + dispatch + backfill × 3 を表現
3. **業務処理と監査書込はアトミック**（成功 / skip パス、同 tx 内）
4. **失敗時は別 tx で DB に永続化**（Task 層の `except` 節で `_record_failure_event` 呼び出し）
5. **3 段防御**（DB 主、ログ副、症状検知が最終バックストップ）
6. **AI raw response / 入力 snapshot を payload で永続化**（プロンプトインジェクション検知）
7. **`fetch_logs` は撤去済み**（z7_drop_fetch_logs migration、読み手ゼロのまま並走期間を経ず 1 PR で物理 drop）

---

## 設計原則

### 第一原理

> **未来の自分（全てを忘れた状態）が、3 ヶ月後にこの行 1 つを見て、何が起きたかを再構成できるか。**

判定期間は **3 ヶ月後**。深い記憶補完が効かなくなる時点。

### データのライフサイクル

監査に載せたい全データを「情報源」「失われる契機」「捕獲できる層」で分類する。これが責務分担を自動的に決める。

| カテゴリ | 例 | 失われる契機 | 復元可否 | 捕獲層 |
|---|---|---|---|---|
| **A: DB 永続** | `article_id`, `source_id`, `news_sources.name` | source 削除 / rename | JOIN で取れる（ただし当時値は失われる） | Repository 補完可 |
| **B: 設定値** | `prompt_version`, `ai_model` | deploy 切替 | 後では取れない | Service（settings 経由）|
| **C: ランタイム文脈** | `attempt`, `trace_id`, 例外オブジェクト | 関数フレーム退出 | 完全に失われる | Task / Service / Repository |
| **D: 外部応答** | HTTP response, AI raw response, 入力 content snapshot | レスポンス GC、DB 上書き | 完全に失われる | Service（業務戻り値経由）|
| **E: Service 内部状態** | Outcome variant 各フィールド, extractor class 名 | execute 退出 | Outcome に乗らないと失われる | Service 内 |

**原則**：
1. 失われると取り返せない情報（B / C / D）は、**捕まえた層が payload に焼き付ける**
2. 後から復元可能な情報（A）は **Repository が補完する**セーフティネット
3. 処理時間は **Logfire / OTel span duration** で見る
4. Service は **taskiq / Logfire に依存しない**（`attempt` はパススルー引数、`trace_id` は Repository 内で完結）

### ランク基準

| ランク | 意味 | 載せる |
|---|---|---|
| **S** | 失敗時の構造的詳細（error_*, HTTP snapshot, AI raw response 等）| 必ず |
| **A** | FK 切断耐性 / 横断検索の主キー（source_id, source_name, prompt_version, content_hash 等）| 必ず |
| **A'** | 当時値の moment-in-time fact（entity_count, candidates_count 等、関連エンティティ更新で失われる）| 必ず |
| **B** | 後から JOIN / 集計で導出可能なもの | **載せない** |

### Outcome の責務純化原則

Service の戻り値型（Outcome）は **「次の段階に渡す価値があるもの」のみを持つ**。観測値（失敗カウンタ、内訳、metadata 等）は監査テーブル（pipeline_events）に焼き付けて消費し、Outcome には含めない。

理由：
- Outcome に観測値が同居すると、**型保証（dispatch 用）と観測値（監査用）が責務混在**になる
- 観測値の集計は SQL で再構成可能なので、Outcome に持つ必要がない
- これにより Outcome は **型ベースの dispatch passport** に責務が純化される

例（Stage 1）：
- ❌ `IngestedOutcome(persisted, staged, failed_count, skipped_count)` ← counts が責務違反
- ✅ `IngestedOutcome(persisted, staged)` ← 「次に渡す価値あるもの」のみ
- 失敗内訳（`failed_codes`）は Service が同 tx で `pipeline_events.payload` に焼き付け

`feedback_responsibility_by_purpose`（目的が違う責務は別クラスに）の自然な適用。

### 「outcome_code は分類、件数は payload」

`outcome_code` は **業務イベントの分類**であって、**数値や程度を表現しない**。「成功した」「成功したが空だった」「成功して大量だった」を別 code に分けない。**数値は payload の責務**。

例（Stage 1）：
- ❌ `outcome_code='fetched'` と `outcome_code='fetched_empty'` を分ける
- ✅ `outcome_code='fetched'` 1 本、件数は `payload.persisted_count` / `payload.staged_count` で判別

### 何を載せないか

- 個人情報・認証情報・cookies
- 完全な HTTP レスポンスボディ（先頭 500 字 / 2KB のみ）
- DB 上で別テーブルに完全に残るもの（articles 本文全体など）
- LLM プロンプトの完全テキスト（`prompt_version` で deploy SHA を載せ、テンプレは git で復元）

### NULL は仕様

stage / event_type の組合わせによっては値が無い列がある。NOT NULL 制約は envelope 必須列のみ。

### Append-only

更新・削除は禁止。誤った行は **補正イベント**（`outcome_code='correction:*'`）を append する。

### 監査失敗が本業を止めない

3 段防御で対応する：
- 第 1 防御（DB）が失敗しても業務 tx は続行
- 第 2 防御（log）で必ず可視化
- 第 3 防御（症状検知）で最終バックストップ

詳細は **書込パターン** 節参照。

---

## アーキテクチャ

### Bounded Context

```
app/observability/
├── __init__.py
├── domain/
│   ├── __init__.py
│   ├── event.py          ← Stage / EventType enum
│   └── payloads.py       ← 全 Payload + Discriminated Union
├── repository.py         ← PipelineEventRepository
└── recording.py          ← _record_failure_event ヘルパー（Task 共通）

app/models/
└── pipeline_event.py     ← SQLAlchemy ORM
```

### 依存方向

```
collection / analysis ──→ observability   (許可、observability は upstream)
observability ──→ collection / analysis    (禁止)
```

---

## データモデル

### Envelope（共通カラム）

| カラム | 型 | NULL | デフォルト | 用途 |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `occurred_at` | timestamptz | NOT NULL | `now()` | event 発生時刻 |
| `stage` | text | NOT NULL | — | CHECK で 9 値 |
| `event_type` | text | NOT NULL | — | CHECK で 4 値 |
| `outcome_code` | text | NOT NULL | — | 業務識別子 |
| `source_id` | int | NULL | — | FK news_sources(id) ON DELETE SET NULL、全 Stage で denormalize |
| `article_id` | int | NULL | — | FK articles(id) ON DELETE SET NULL |
| `attempt` | smallint | NOT NULL | 1 | taskiq retry_count + 1 |
| `error_class` | text | NULL | — | Python FQN（failed のみ）|
| `trace_id` | text | NULL | — | Logfire trace ID。execute span から Repository 内で補完 (cron tick の batch trace、span 外は NULL) |
| `payload` | jsonb | NOT NULL | `'{}'` | Stage 別 Pydantic |

`trace_id` は観測 B軸 (cron tick を根とする batch trace の Logfire ポインタ) 専用。記事単位の
相関 (A軸) は `article_id` が担う。将来ドメインの lifecycle 相関を導入する場合は
`correlation_id` / `pipeline_run_id` 系の列名にし、`trace` 語は使わない。

### Stage / EventType enum（CHECK 制約）

```python
class Stage(StrEnum):
    DISPATCH = "dispatch"
    SOURCE_FETCH = "source_fetch"
    CONTENT_FETCH = "content_fetch"
    EXTRACTION = "extraction"
    CLASSIFICATION = "classification"
    EMBEDDING = "embedding"
    BACKFILL_EXTRACT = "backfill_extract"
    BACKFILL_CLASSIFY = "backfill_classify"
    BACKFILL_EMBED = "backfill_embed"


class EventType(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAILED = "failed"
```

### Payload（Pydantic Discriminated Union）

```python
# app/observability/domain/payloads.py

class BasePipelineEventPayload(BaseModel):
    """共通基底 — A 級保険 + S 級失敗詳細を共通化。"""
    kind: str
    source_name: str | None = None         # A: FK 切断耐性
    error_message: str | None = None       # S: 失敗時
    error_chain: list[str] | None = None   # S: cause chain FQN list
    model_config = ConfigDict(extra="forbid")


class DispatchPayload(BasePipelineEventPayload):
    kind: Literal["dispatch"] = "dispatch"
    cadence: Literal["high", "medium", "low", "all"] | None = None
    dispatched_count: int | None = None
    selected_count: int | None = None
    rejected_count: int | None = None
    failed_count: int | None = None
    raw_source_name: str | None = None
    skip_reason: Literal["no_active_sources"] | None = None # S


class SourceFetchPayload(BasePipelineEventPayload):
    """Stage 1 (source_fetch) — 1 ソース 1 fetch の集約サマリ。"""
    kind: Literal["source_fetch"] = "source_fetch"
    fetcher_class: str | None = None       # A: type(fetcher).__name__

    # 成功時の集約カウンタ
    persisted_count: int | None = None     # A' (Pattern R 永続化数)
    staged_count: int | None = None        # A' (Pattern H staged 数)
    failed_count: int | None = None        # A' (エントリ単位 Failed 数)
    skipped_count: int | None = None       # A' (race 敗北等)
    failed_codes: dict[str, int] | None = None  # S: Failed.reason.code 別カウント

    # ソースが提供する metadata の観測 (C 案: フィールド名 + 代表サンプル 1 件)
    # 「このソースが何を提供しているか」を後から SQL で集計するための情報
    metadata_fields_observed: list[str] | None = None  # A: 1+ entry で値があったフィールド名
    metadata_sample: dict | None = None    # A': 最初の entry の metadata.model_dump(exclude_none=True)

    # 失敗時の S 級スナップショット (Task 例外パスで詰める、PR1 では NULL)
    http_status: int | None = None
    final_url: str | None = None
    response_size: int | None = None
    content_type: str | None = None
    body_head: str | None = None           # 先頭 500 字


class ContentFetchPayload(BasePipelineEventPayload):
    kind: Literal["content_fetch"] = "content_fetch"
    discovered_article_id: int             # A: article 削除耐性
    extractor_class: str | None = None     # A
    quality_gate_metric: dict | None = None # S（quality_gate skip 時）
    # 失敗時の S 級スナップショット
    http_status: int | None = None
    final_url: str | None = None
    response_size: int | None = None
    content_type: str | None = None
    body_head: str | None = None           # 先頭 500 字


class ExtractionPayload(BasePipelineEventPayload):
    """Stage 3 — 大きい入力（記事本文）が来るので head + length + hash で扱う。"""
    kind: Literal["extraction"] = "extraction"
    ai_model: str | None = None            # S
    prompt_version: str | None = None      # A: prompt+model+gen_config+response_schema+system_instruction の SHA-256 prefix 8

    # 入力（外部由来 raw、article.original_content 経由）
    input_content_head: str | None = None   # S: 先頭 2KB（注入文字列の在処を含む）
    input_content_length: int | None = None # A': 全体長（truncate 検知用）
    input_content_hash: str | None = None   # A: sha256 prefix 16 文字（整合性 + 再実行検証）

    # 出力（AI raw、Vector 内のどこにも残らない極めて貴重な情報）
    ai_raw_response: str | None = None     # S: 2KB 上限

    # 解釈結果
    entity_count: int | None = None        # A'


class ClassificationPayload(BasePipelineEventPayload):
    """Stage 4 — 入力が小さい（記事サマリ）ので full 保存。"""
    kind: Literal["classification"] = "classification"
    ai_model: str | None = None            # S
    prompt_version: str | None = None      # A: call signature hash (Stage 3 と同方式)

    # 入力（4KB hard limit、full）
    input_text: str | None = None          # S: full
    input_text_length: int | None = None   # A'

    # 出力
    ai_raw_response: str | None = None     # S: 2KB
    raw_category: str | None = None        # S
    raw_topic: str | None = None           # S


class EmbeddingPayload(BasePipelineEventPayload):
    """Stage 5 — analysis テキストから vector を生成。raw I/O 捕捉は不要
    （入力は analysis に永続、出力は数値 vector で injection 観点無関係）。"""
    kind: Literal["embedding"] = "embedding"
    embedding_model: str | None = None     # A
    vector_dimension: int | None = None    # A'


PipelineEventPayload = Annotated[
    DispatchPayload | SourceFetchPayload | ContentFetchPayload
    | ExtractionPayload | ClassificationPayload | EmbeddingPayload,
    Field(discriminator="kind"),
]
```

### Stage × EventType × outcome_code マッピング

| Stage | event_type | outcome_code | 場所 |
|---|---|---|---|
| dispatch | succeeded | `dispatched` | Service 同 tx |
| dispatch | skipped | `no_active_sources` | Service 同 tx |
| source_fetch | failed | `permanent_fetch_error` / `temporary_fetch_error_exhausted` / `unexpected_error` | Task except 節 |
| content_fetch | succeeded | `fetched` / `already_fetched` | Service 同 tx |
| content_fetch | skipped | `discovered_not_found` / `permanent_fetch_error` / `not_html` / `parse_error` / `quality_gate` | Service 同 tx |
| content_fetch | failed | `temporary_fetch_error_exhausted` / `unexpected_error` | Task except 節 |
| extraction | succeeded | `extracted` / `extracted_as_noise` | Service 同 tx |
| extraction | skipped | `skipped_invalid_input` | Service 同 tx |
| extraction | failed (内容起因 Permanent → DELETE) | `ai_error_blocked_by_policy` / `ai_error_input_too_large` | Service `mark_article_unprocessable` 同 tx |
| extraction | failed (環境起因 Permanent、記事保持) | `ai_error_config` / `ai_error_insufficient_balance` | Task except 節 |
| extraction | failed (Transient、cron 救済) | `ai_error_provider` / `ai_error_rate_limited` / `ai_error_daily_quota_exhausted` / `ai_error_network` / `unclassified_error` / `unexpected_error` | Task except 節 |
| classification | succeeded | `classified` / `already_classified` | Service 同 tx |
| classification | rejected | `out_of_scope` / `already_rejected` | Service 同 tx |
| classification | skipped | `extraction_pending` / `not_found` | Service 同 tx |
| classification | failed | `ai_error_exhausted` / `unexpected_error` | Task except 節 |
| embedding | succeeded | `embedded` / `already_embedded` | Service 同 tx |
| embedding | skipped | `extraction_not_found` / `analysis_pending` / `analysis_rejected` / `invalid_input` | Service 同 tx |
| embedding | failed | `ai_error_exhausted` / `unexpected_error` | Task except 節 |
| backfill_* | succeeded | `backfill_item_enqueued` / `backfill_run_completed` | Task orchestration 別 tx |
| backfill_* | skipped | `backfill_run_no_targets` / `backfill_run_kill_switch_disabled` / `backfill_run_held_by_stage_hold` / `backfill_run_daily_budget_exhausted` | Task orchestration 別 tx |
| backfill_* | failed | `backfill_item_enqueue_failed` / `backfill_run_failed` | Task orchestration 別 tx |
| trend_discovery | succeeded | `trend_discovery_run_completed` / `trend_discovery_run_updated` | Task / CLI orchestration 別 tx |
| trend_discovery | skipped | `trend_discovery_run_no_target_articles` / `trend_discovery_run_already_exists` / `trend_discovery_run_conflict` | Task / CLI orchestration 別 tx |
| trend_discovery | failed | `trend_discovery_run_failed` | Task / CLI orchestration 別 tx |

> **注**: `source_fetch.succeeded` audit は撤去済 (中途半端な構造を再設計するため)。失敗系 (`permanent_fetch_error` / `temporary_fetch_error_exhausted` / `unexpected_error`) のみ Task 層から書込中。再導入時は集計単位と分類コードを整理して入れ直す。

### Index 戦略

```sql
CREATE INDEX idx_pipeline_events_occurred_at_brin ON pipeline_events USING BRIN (occurred_at);
CREATE INDEX idx_pipeline_events_stage_outcome ON pipeline_events (stage, event_type, outcome_code, occurred_at DESC);
CREATE INDEX idx_pipeline_events_article_id ON pipeline_events (article_id, occurred_at DESC) WHERE article_id IS NOT NULL;
CREATE INDEX idx_pipeline_events_source_id ON pipeline_events (source_id, occurred_at DESC) WHERE source_id IS NOT NULL;
CREATE INDEX idx_pipeline_events_failed ON pipeline_events (occurred_at DESC) WHERE event_type = 'failed';
CREATE INDEX idx_pipeline_events_payload_gin ON pipeline_events USING GIN (payload jsonb_path_ops);
```

### CHECK 制約

```sql
ALTER TABLE pipeline_events ADD CONSTRAINT ck_stage CHECK (
    stage IN (
        'dispatch', 'source_fetch', 'content_fetch',
        'extraction', 'classification', 'embedding',
        'backfill_extract', 'backfill_classify', 'backfill_embed'
    )
);

ALTER TABLE pipeline_events ADD CONSTRAINT ck_event_type CHECK (
    event_type IN ('succeeded', 'skipped', 'rejected', 'failed')
);

ALTER TABLE pipeline_events ADD CONSTRAINT ck_attempt_positive CHECK (attempt >= 1);
```

---

## AI prompt / response の捕捉（プロンプトインジェクション対応）

### 動機

- AI 入力（記事本文 / サマリ）は `articles.original_content` に残るが、**監査時点の値**は再抽出で書き換わる可能性
- AI 出力（raw response）は **Vector のどこにも永続化されない**。インジェクション検知の決め手になる
- prompt 自体は `prompt_version`（deploy SHA）で git から復元

### 採用するフィールド

#### Stage 3 (ExtractionPayload) — 大きい入力に対応

| フィールド | サイズ | ランク | 理由 |
|---|---|---|---|
| `input_content_head` | 先頭 2KB | S | 注入文字列は冒頭に置かれる傾向、後段でも head に痕跡が残る |
| `input_content_length` | int | A' | truncate 検知 |
| `input_content_hash` | sha256 prefix 16 文字 | A | 再実行時の同一性確認、改竄検知 |
| `ai_raw_response` | 先頭 2KB | S | 出力でのインジェクション成功痕跡（system prompt 漏洩等）|

#### Stage 4 (ClassificationPayload) — 小さい入力なので full 保存

| フィールド | サイズ | ランク | 理由 |
|---|---|---|---|
| `input_text` | full（4KB hard limit）| S | サマリは小さい、full で injection を完全に再現 |
| `input_text_length` | int | A' | hard limit に当たったか判別 |
| `ai_raw_response` | 先頭 2KB | S | 同上 |

### 捕捉ポリシー

- **always-on**：成功時も含めて毎回捕捉。失敗時のみだと "stealth injection"（出力を歪めるが例外は出さない）を取り逃がす
- **AI client の戻り値を拡張する必要あり**：raw response を業務戻り値に含めて Service に渡す（PR3 で実装）

### prompt_version の規律

- **call signature hash 方式** で構造的に算出する。`prompt_template + model + gen_config + response_schema + system_instruction` の 5 要素を SHA-256 で hash し prefix 8 文字を採る
- 5 要素は LLM 呼出条件の再現性に効く全要素であり、いずれかが変わると hash が変わる ⇔ 同じ hash なら呼出条件が完全一致
- 各 (stage, provider) ごとに **Prompt class**（`GeminiExtractionPrompt` / `GeminiClassificationPrompt` / `DeepSeekClassificationPrompt`）が ClassVar で 5 要素を保持し、class load 時に `VERSION` ClassVar として hash を確定させる（外部代入 / decorator 不要）
- git short SHA 注入は採らない。プロンプトを変えていない commit でも値が変わるノイズを生み、`prompt_version 別の OOS 率` 等の SQL 集計が薄まる
- 人間が手で bump する `_PROMPT_VERSION = "v4"` 文字列も採らない。確実に乖離する（忘れる / 過剰更新）
- `gen_config` は `MappingProxyType` で immutable にし、書換による silent audit lying を構造的に排除する
- Pydantic の minor version bump で `model_json_schema()` 出力 dict の構造が変わる可能性は **noise として許容**（false positive で hash が変わる、頻度は低い）
- Phase 2 として、`<untrusted_input>` ブロックに渡す入力を `UntrustedStr` / `TrustedStr` の typed boundary で強制する案を検討する（render() の typed kwargs だけでは「sanitize 忘れ」を完全には防げない）

### content_hash の位置づけ（Stage 3 のみ）

目的は 2 つ：
1. **整合性**：記録した head と全体の対応関係を確認（head だけでは全体は分からない）
2. **再実行検証**：3 ヶ月後に当該記事を再分析した結果が今回と同じ入力か検証

副作用として改竄検知にもなる（ただし同じ DB 内なので、DB ごと改竄されたら無効）。

### Volume 試算

| Stage | 1 件あたり payload | 年間件数 | 容量 |
|---|---|---|---|
| dispatch | 100 B | 10,000 | 1 MB |
| source_fetch | 500 B（成功）/ 1KB（失敗） | 30,000 | 20 MB |
| content_fetch | 500 B / 2KB | 50,000 | 50 MB |
| extraction | 5KB（head 2KB + raw 2KB + meta） | 5,000 | 25 MB |
| classification | 8KB（input 4KB + raw 2KB + meta） | 5,000 | 40 MB |
| embedding | 200 B | 5,000 | 1 MB |
| backfill × 3 | 100 B | 30,000 | 3 MB |
| **合計** | | **約 135,000** | **約 140 MB / 年** |

PostgreSQL のサイズとして十分小さい。toast / 圧縮で実効はさらに小さい。

### 期待されるクエリ例

```sql
-- どの source が最も失敗するか（過去 7 日）
SELECT
    pe.source_id,
    ns.name,
    pe.outcome_code,
    count(*) AS failures
FROM pipeline_events pe
LEFT JOIN news_sources ns ON ns.id = pe.source_id
WHERE pe.event_type = 'failed' AND pe.occurred_at > now() - interval '7 days'
GROUP BY pe.source_id, ns.name, pe.outcome_code
ORDER BY failures DESC;

-- 特定記事の処理履歴
SELECT pe.occurred_at, pe.stage, pe.event_type, pe.outcome_code, pe.payload
FROM pipeline_events pe
WHERE pe.article_id = $1
ORDER BY pe.occurred_at;

-- prompt_version 別の Out-of-scope 率
SELECT
    pe.payload->>'prompt_version' AS pv,
    count(*) FILTER (WHERE pe.event_type = 'rejected') * 1.0 / count(*) AS oos_rate
FROM pipeline_events pe
WHERE pe.stage = 'classification' AND pe.occurred_at > now() - interval '30 days'
GROUP BY pv
ORDER BY pv;

-- AI raw response にプロンプト漏洩の痕跡が無いか
SELECT pe.id, pe.occurred_at, pe.article_id, pe.payload->>'ai_raw_response' AS resp
FROM pipeline_events pe
WHERE pe.stage IN ('extraction', 'classification')
  AND pe.payload->>'ai_raw_response' ILIKE '%system%prompt%'
LIMIT 100;

-- 失敗監査が落ちた可能性（業務テーブルの "穴" による症状検知）
SELECT a.id, a.created_at
FROM articles a
LEFT JOIN extractions e ON e.article_id = a.id
LEFT JOIN pipeline_events pe ON pe.article_id = a.id AND pe.stage = 'extraction'
WHERE a.created_at < now() - interval '24 hours'
  AND e.id IS NULL
  AND pe.id IS NULL
LIMIT 100;
```

---

## 書込パターン

### 同 tx 原則（成功 / skip パス）

業務処理と監査書込は **同一トランザクション内** で完了させる。これによって：

- 業務 commit ⇔ 監査存在 が常に一致（取りこぼしゼロ）
- 業務 rollback で監査も消える（業務がなかったことになるなら監査も無くて良い）

```python
class ContentFetchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        html_extractor: ArticleHtmlExtractor,
    ) -> None:
        self._session_factory = session_factory
        self._html_extractor = html_extractor

    async def execute(
        self,
        discovered_article_id: int,
        *,
        attempt: int = 1,
    ) -> ContentFetchOutcome:
        async with self._session_factory() as session:
            # ① 業務処理
            outcome = await self._do_business_logic(session, discovered_article_id)

            # ② 同じ session で監査 INSERT（ヘルパーは Service 内 private）
            event_repo = PipelineEventRepository(session)
            await self._record_event(event_repo, outcome, attempt)

            # ③ アトミックに commit
            await session.commit()
            return outcome

    async def _record_event(
        self,
        event_repo: PipelineEventRepository,
        outcome: ContentFetchOutcome,
        attempt: int,
    ) -> None:
        """Outcome variant を網羅して 1 行 INSERT。"""
        match outcome:
            case ContentFetchedOutcome(article=article):
                await event_repo.append(
                    stage=Stage.CONTENT_FETCH,
                    event_type=EventType.SUCCEEDED,
                    outcome_code="fetched",
                    article_id=article.id,
                    attempt=attempt,
                    payload=ContentFetchPayload(
                        kind="content_fetch",
                        discovered_article_id=outcome.lookup_id,
                        extractor_class=type(self._html_extractor).__name__,
                    ),
                )
            case AlreadyFetchedOutcome(article=article):
                ...
            case ContentFetchSkippedOutcome(reason=reason):
                ...
```

### 例外パス（Task 主導、別 tx）

Service が例外を raise すると、Service の session は context manager で rollback される。**Task 層が新 session を開いて監査を書く**。

「失敗」を 3 種類に区別：

| 種類 | 例 | 書込場所 |
|---|---|---|
| A. Service が例外 raise | TemporaryFetchError, PermanentFetchError, 想定外 | **Task の `except` 節**（別 session）|
| B. Service が "失敗系 Outcome" を返す | ContentFetchSkippedOutcome | **Service 内 `_record_event`**（同 tx）|
| C. 業務 commit 後の失敗 | commit 後の Task 障害 | best-effort（諦め）|

```python
# app/collection/tasks.py
@broker_content.task(
    task_name="fetch_content",
    timeout=60,
    max_retries=3,
    retry_on_error=True,
)
async def fetch_content(
    discovered_article_id: int,
    ctx: Context = TaskiqDepends(),
) -> int | None:
    attempt = (ctx.message.labels.get("retry_count") or 0) + 1
    is_last_attempt = attempt >= 3

    svc = ctx.state.content_fetch_service
    session_factory = ctx.state.session_factory

    try:
        outcome = await svc.execute(discovered_article_id, attempt=attempt)
        return _extract_article_id(outcome)

    except TemporaryFetchError as e:
        if is_last_attempt:
            await _record_failure_event(
                session_factory=session_factory,
                stage=Stage.CONTENT_FETCH,
                outcome_code="temporary_fetch_error_exhausted",
                discovered_article_id=discovered_article_id,
                exc=e,
                attempt=attempt,
            )
            return None
        raise  # 中間 attempt は taskiq retry に任せる

    except PermanentFetchError as e:
        await _record_failure_event(
            session_factory=session_factory,
            stage=Stage.CONTENT_FETCH,
            outcome_code="permanent_fetch_error",
            discovered_article_id=discovered_article_id,
            exc=e,
            attempt=attempt,
        )
        return None

    except Exception as e:
        await _record_failure_event(
            session_factory=session_factory,
            stage=Stage.CONTENT_FETCH,
            outcome_code="unexpected_error",
            discovered_article_id=discovered_article_id,
            exc=e,
            attempt=attempt,
        )
        raise
```

### `_record_failure_event` 共通ヘルパー

各 Task で重複しないよう Stage 横断ヘルパーを置く：

```python
# app/observability/recording.py
async def _record_failure_event(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    stage: Stage,
    outcome_code: str,
    exc: Exception,
    attempt: int,
    discovered_article_id: int | None = None,
    article_id: int | None = None,
    source_id: int | None = None,
    payload_extra: dict | None = None,
) -> None:
    """例外パス専用の監査書込ヘルパー。新 session で別 tx。

    第 1 防御: DB INSERT
    第 2 防御: 失敗時 structlog で fallback
    """
    error_class = f"{type(exc).__module__}.{type(exc).__name__}"
    error_chain = _extract_error_chain(exc)

    payload = build_failure_payload(
        stage=stage,
        error_message=str(exc),
        error_chain=error_chain,
        discovered_article_id=discovered_article_id,
        extra=payload_extra,
    )

    try:
        async with session_factory() as session:
            event_repo = PipelineEventRepository(session)
            await event_repo.append(
                stage=stage,
                event_type=EventType.FAILED,
                outcome_code=outcome_code,
                article_id=article_id,
                source_id=source_id,
                attempt=attempt,
                error_class=error_class,
                payload=payload,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "pipeline_event_append_failed_in_exception_path",
            stage=stage.value,
            outcome_code=outcome_code,
            article_id=article_id,
            discovered_article_id=discovered_article_id,
            business_error_class=type(exc).__name__,
            business_error_message=str(exc),
            audit_error=str(audit_exc),
        )
```

### 3 段防御

```
[失敗発生]
  ↓
[第 1 防御] 別 session で DB INSERT          ← 主、SQL 検索可、長期保持
  ↓ それが失敗したら
[第 2 防御] structlog で構造化ログ出力       ← 副、fallback、短期保持
              （業務エラー + 監査エラーを必ず両方出す）
  ↓ それも消えたら
[第 3 防御] articles の "穴" として症状検知  ← 最終バックストップ
              （extraction NULL の article 数 SQL）
```

**「ログだけでは不十分」の理由**：
1. SQL 集計・JOIN・時系列分析ができない
2. キー名のブレで検索不能になり、構造的保証がない
3. 保持期間が短く（30〜90 日）、3 ヶ月後の遡及調査に間に合わない
4. 業務テーブル（articles）と紐付けたクエリが書けない
5. ダッシュボード化が困難

**「完全な監査は不可能」の宣言**：失敗監査の書込自体が失敗する可能性は理論的に排除不能（無限後退）。本 ADR は **「成功は強整合、失敗は best-effort + 3 段防御」** という非対称設計を選択する。

### Repository 設計

```python
# app/observability/repository.py
class PipelineEventRepository:
    """stateless、session を ctor で受ける（Vector 既存 Repository 流儀）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        stage: Stage,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        attempt: int = 1,
        error_class: str | None = None,
    ) -> None:
        # source_id 自動補完: article_id から逆引き
        if source_id is None and article_id is not None:
            source_id = await self._session.scalar(
                select(Article.news_source_id).where(Article.id == article_id)
            )

        # trace_id 補完（OTel current span から読込）
        trace_id = self._get_current_trace_id()

        event = PipelineEvent(
            stage=stage.value,
            event_type=event_type.value,
            outcome_code=outcome_code,
            source_id=source_id,
            article_id=article_id,
            attempt=attempt,
            error_class=error_class,
            trace_id=trace_id,
            payload=payload.model_dump(),
        )
        self._session.add(event)

    @staticmethod
    def _get_current_trace_id() -> str | None:
        """現在 active な OTel span の trace_id を W3C 32-hex (小文字) で返す (span 外は None)。"""
        span_context = trace.get_current_span().get_span_context()
        return f"{span_context.trace_id:032x}" if span_context.is_valid else None
```

### 共通ルール

- Service ctor で受けるのは **`session_factory` のみ**（`feedback_session_factory_di`）
- Repository は **session を ctor で受け、append は instance method**（既存 Repository と同流儀）
- Pydantic payload を作る責務は **Service**（Outcome variant 知識の所在）
- Service は **`taskiq` / `Logfire` に依存しない**（attempt はパススルー、trace_id は Repository 補完）
- 失敗時の log では **業務エラー + 監査エラーを必ず両方出す**

---

## fetch_logs の処遇

**完了 (z7_drop_fetch_logs migration, 2026-05-26)**。`fetch_logs` テーブルは
`pipeline_events.stage='acquisition'` への per-article SUCCEEDED + per-failure
FAILED 焼き付けで完全代替され、読み手ゼロのまま物理 drop した。

本番未 deploy (fly.toml `[processes]` に worker/scheduler 未配線) でデータ消失
リスクが無かったため、当初計画の PR5 (アプリ撤去) + PR6 (テーブル drop) 分割は
1 PR に集約した (並走期間不要)。

`articles_count` の事前計算済み指標は失ったが、pipeline_events を
`event_type='succeeded' AND outcome_code IN ('article_created',
'incomplete_article_created')` で COUNT すれば導出可能。集計 consumer 出現時に
焼き直す方針 (consumer-driven audit scope)。処理時間は Logfire taskiq span
(Phase 3 trace 伝搬) の duration を見る。

---

## PR 分割ロードマップ

| PR | 内容 | 規模 |
|---|---|---|
| **PR1** | 監査基盤導入: `pipeline_events` 表 Alembic + ORM + `app/observability/` (domain / repository / recording) + ADR + Stage 1 (`source_fetch`) 統合。`SourceFetchPayload` の `metadata_*` フィールドは用意するが NULL のまま | 中 |
| **PR1.5** | 型階層整理: `ReadyForArticle` を「永続化保証型」として 1 本化（`FetchedArticle` 廃止統合 / `metadata` 削除）、`FetchedEntry` envelope 新設、19 Fetcher の戻り型修正、`ArticleAcquisitionService` で metadata 観測 → `metadata_fields_observed` / `metadata_sample` に値が入り始める | 中 |
| **PR2** | Stage 2 (`content_fetch`) 統合 | 中 |
| **PR3** | Stage 3〜5 (extraction / classification / embedding) — Stage 3 を Outcome 化する PR を含む。**AI client / extractor の戻り値拡張**（raw response / 入力 snapshot を業務戻り値に含める）もここで導入 | 大 or 分割 |
| **PR4** | dispatch + backfill 統合 | 小 |
| **PR5 + PR6** (集約) | fetch_logs 書込撤去 + テーブル drop (本番未 deploy のため 1 PR、`z7_drop_fetch_logs`) | 小 |

PR1 + PR1.5 + PR2 で payload 形を実運用検証してから PR3 以降に進む。

### PR1 と PR1.5 を分ける理由

- PR1 は「監査基盤の導入」が焦点。Fetcher / 型階層リファクタが混ざるとレビューが難しい
- PR1.5 は domain BC の変更なので独立 PR で `domain-reviewer` レビューを挟みたい
- PR1 では `metadata_fields_observed` / `metadata_sample` は payload 構造に含めるが NULL（`ReadyForArticle.metadata` がまだ存在）、PR1.5 で値が入り始める

---

## 既決事項

| 論点 | 決定 |
|---|---|
| v1 スコープ | 全 Stage、6 PR 分割 |
| Payload 配置 | 集約（`app/observability/`）|
| Stage 3 (extraction) Outcome 化 | v1 で実施 |
| 同 tx 原則 | 成功 / skip パスは業務 + 監査をアトミックに同 tx |
| 例外パス書込 | Task 層が新 session で INSERT（`_record_failure_event` 共通ヘルパー）、失敗は log + 続行 |
| 3 段防御 | DB（主）→ log（副）→ 症状検知（最終）|
| 「ログだけ」却下 | SQL 集計 / JOIN / 長期保持 / 構造保証が不可能なため |
| 失敗の完全性 | 「成功は強整合、失敗は best-effort」を非対称で受け入れる |
| 試行 + 結果モデル却下 | taskiq broker が試行を Redis で保持済み、二重記録は過剰 |
| 二系統（DB + ログ基盤）却下 | Vector のスケールでは過剰、structlog で fallback すれば十分 |
| 冪等ヒット | succeeded + outcome_code='already_*' で記録 |
| dispatch / backfill | event 化する |
| fetch_logs | 撤去済み (z7_drop_fetch_logs) |
| Source_id denormalize | 全 Stage で実施、Repository が article_id から自動補完 |
| ORM 配置 | `app/models/pipeline_event.py` |
| 第一原理 | 「未来の自分（全てを忘れた状態）」、判定期間「3 ヶ月後」|
| データのライフサイクル原則 | 失われると取り返せない情報（B/C/D）は捕まえた層で焼き付け、復元可能（A）は Repository が補完 |
| ランク基準 | S / A / A' / B、Vector では B 級は原則載せない |
| Repository 設計 | session を ctor で受け、append は instance method（Vector 既存流儀）|
| Service DI | `event_repo` は ctor 注入せず、Service 内で都度 `PipelineEventRepository(session)` |
| Outcome → payload 変換 | Service 内 private `_record_event(outcome, ...)` で `match` 網羅 |
| 横断情報 | attempt=Task→Service 引数 / error_class=Task / trace_id=Repository |
| prompt_version | call signature hash 方式（prompt+model+gen_config+response_schema+system_instruction の SHA-256 prefix 8）。Prompt class（per stage × provider）が ClassVar で確定。git SHA 注入は採らない |
| AI raw I/O 捕捉 | always-on、Stage 3 は head + length + hash + raw 2KB、Stage 4 は input full + raw 2KB |
| AI client 戻り値拡張 | PR3 で実施（raw response / 入力 snapshot を業務戻り値に含める）|
| 例外パス D 級情報 | 「業務 raise 後に Task で書く」構造上、AI raw 等 D 級は載らない（best-effort）|
| Outcome の責務純化 | Service 戻り値は「次の段階に渡す価値があるもの」のみ、観測値は監査テーブルに焼き付け |
| outcome_code は分類のみ | 件数・程度を outcome_code に持たせない（`fetched_empty` 等は不採用、件数は payload）|
| Stage 1 の "永続化保証型" 一本化 | `ReadyForArticle` に統合（`FetchedArticle` 廃止）、metadata は型から削除（PR1.5）|
| Stage 1 metadata 観測 | C 案: `metadata_fields_observed`（取れたフィールド名 list）+ `metadata_sample`（最初の 1 件 dump）|
| `FetchedEntry` envelope | Fetcher が yield する 1 entry の運搬箱（`item: FetchedItem` + `metadata: FetchedMetadata`）。業務責務と監査責務を entry レベルで分離しつつ対応関係を保つ |
| Stage 1 outcome_code | `fetched`（成功時）+ `permanent_fetch_error` / `temporary_fetch_error_exhausted` / `unexpected_error`（失敗時）の 4 種のみ |
| Stage 3 outcome_code (PR3-a-1) | 13 個に拡張: 成功 2 (`extracted` / `extracted_as_noise`)、skip 1 (`skipped_invalid_input`)、内容起因 Permanent (DELETE 対象) 2 (`ai_error_blocked_by_policy` / `ai_error_input_too_large`)、環境起因 Permanent (記事保持) 2 (`ai_error_config` / `ai_error_insufficient_balance`)、Transient (cron 救済) 6 (`ai_error_provider` / `ai_error_rate_limited` / `ai_error_daily_quota_exhausted` / `ai_error_network` / `unclassified_error` / `unexpected_error`)。詳細は `specs/pipeline-events-stage3-design.md`。 |
| Stage 3 reason_code を payload に追加しない (PR3-a-1) | 二層設計 (top-level `outcome_code` + payload free-form `error_chain` / `error_message`) で十分。`reason_code` を別 field として導入すると分類が outcome_code と二重化し、責務境界が崩れる。 |
| Stage 3 DELETE 規律 (PR3-a-1) | 内容起因 Permanent failure (`ai_error_blocked_by_policy` / `ai_error_input_too_large`) のみ、`ExtractionService.mark_article_unprocessable()` が 1 tx 内で **audit INSERT 先 → DELETE 後** で実行する。`pipeline_events.article_id` は `ondelete=SET NULL`、`source_id` は INSERT 時に auto-resolve 済のため article DELETE 後も起点 source 追跡可能。環境起因 Permanent (config / 残高) と Transient は記事を残す (人間対応 / cron 救済)。 |
| Stage 3 inline retry 範囲 (PR3-a-1) | `NetworkError` / `ProviderError` のみ `max_retries=1` で taskiq retry。それ以外 (rate limit / daily quota / config / insufficient balance / unclassified / unexpected) は即 audit 焼付けて return → 救済 cron (PR3-a-2) 委譲。 |

---

## 残された未決定事項

1. **補正イベント outcome_code prefix 規約**（例：`correction:*`）— 運用で必要になったら追加
2. **Logfire instrumentation（B 層）と検知層（C1/C2/C3）の導入時期** — post-v1
3. **第 3 防御の SQL 群整備時期** — v1 同梱 or post-v1（運用上の優先度で判断）

---

## 参照

- `feedback_session_factory_di` — Service には session_factory（async_sessionmaker）を DI
- `feedback_responsibility_by_purpose` — 目的が違う責務は別クラス / ファイルに分離
- `feedback_no_share_different_problems` — 似ていても問題が違うなら共用しない
- `feedback_failure_visibility` — 故障の見える化を優先、自己修復 fallback で隠蔽しない
- `feedback_snapshot_responsibility` — snapshot は 1 単位保存が責務、JSONB 1 カラム + メタが原則
- `feedback_structural_guarantee` — 不変条件はランタイムチェックでなく DB 制約 + ファクトリで構造的に強制
- CLAUDE.md 禁止事項 6 — グローバルシングルトン禁止
- ライフサイクル整理（本 ADR §設計原則）— 設計含意の出発点
