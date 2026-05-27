# Pipeline Events Failure Attributes

## 現行契約

`pipeline_events` の失敗属性は、撤去済みの旧 top-level 3 列を使わず、
次の契約で読む。

- `outcome_code`: 何が起きたかを表す唯一の event code
- `retryability`: 同一入力の将来再実行で回復しうるかを表す横断軸
- `payload.failure_kind`: stage-local な失敗種別
- `payload.failure_action`: 明示的な業務副作用
- `payload.attempt_count`: completion の claim 試行番号 snapshot

`error_class` は runtime FQN の forensic 情報であり、主契約にはしない。

## 設計原則

`pipeline_events` は、その時点で起きたイベントの immutable snapshot とする。
error class / adapter は write-time の SSoT だが、過去 event を後から SQL で
読めるように、横断検索したい属性だけを top-level column に焼く。

- 全 stage で同じ意味を持ち、横断検索・集計したい属性は top-level column。
- stage ごとに意味が変わる属性、または stage 固有の詳細は `payload` JSONB。
- `payload` は詳細 snapshot であり、全 stage 共通の状態分類を押し込まない。

## Top-level Columns

```text
id
occurred_at
stage
event_type
outcome_code
retryability nullable
source_id nullable
article_id nullable
error_class nullable
trace_id nullable
payload jsonb
```

`trace_id` は optional な observability infrastructure 用の列で、失敗属性
projection の主契約ではない。処理時間は Logfire / OTel span duration を見る。

## Payload JSONB

失敗 event では、stage payload に以下を追加できる。

```json
{
  "failure_kind": "terminal_drop",
  "failure_action": "drop_article",
  "error_message": "...",
  "error_chain": [
    "app.analysis.curation.errors.CurationTerminalDropError",
    "app.analysis.ai_provider_errors.AIProviderInputRejectedError"
  ]
}
```

`failure_kind` / `failure_action` は stage と組で意味が決まるため、top-level
column にはしない。

completion は Ready を伴う行で `payload.attempt_count` を持つ。これは
`incomplete_articles.attempt_count` の監査時点 snapshot で、retry budget /
stale worker guard の業務制御に由来する。completion 以外の stage には
attempt 系 payload を追加しない。

## 用語

### outcome_code

イベントで「何が起きたか」を表す唯一の code。

失敗 event では、error が持つ code をそのまま焼く。

例:

- `ai_error_rate_limited`
- `assessment_category_missing`
- `embedding_response_invalid`
- `db_runtime_error`
- `unexpected_error`

成功・棄却・skip event では、イベント結果の code を焼く。

例:

- `curated_signal`
- `assessed_out_of_scope`
- `briefing_input_empty`
- `stale_attempt`

### retryability

同一入力を将来再実行したときに回復しうるか、という横断軸。

値:

- `retryable`
- `non_retryable`
- `unknown`

retry 上限に到達した事実は intrinsic な retryability ではないため、必要な stage は
`payload.retry_exhausted` で表す。

### failure_kind

失敗の stage-local な種類。

例:

- curation: `terminal_drop` / `terminal_keep` / `recoverable`
- assessment: `terminal_skip` / `recoverable`
- embedding: `terminal_skip` / `recoverable`
- briefing: `configuration` / `response_invalid` / `llm_error`
- DB adapter: `db_runtime` / `db_constraint` / `db_query_or_schema` / `db_unknown`

### failure_action

失敗に伴う stage 固有の業務副作用。

現時点で明示する値は `drop_article` のみ。`keep_article` /
`keep_curation` のような「保持」は明示 action としては扱わない。

## Error Class Contract

marker error class は write-time SSoT として以下を持つ。

```python
class CurationTerminalDropError(...):
    STAGE = Stage.CURATION
    FAILURE_KIND = "terminal_drop"
    RETRYABILITY = Retryability.NON_RETRYABLE
    FAILURE_ACTION = FailureAction.DROP_ARTICLE
```

`code` / `provider_error` は instance 属性として維持する。DB 書き込み時は
projection helper が error class / DB error / catch-all から失敗属性を作り、
`outcome_code` / `retryability` / payload に展開する。

## Stage 1 / Stage 2 Adapter Policy

Stage 1 / Stage 2 の取得系 error には、`STAGE` / `FAILURE_KIND` /
`RETRYABILITY` / `FAILURE_ACTION` を無理に追加しない。

`ExternalFetchError` family は origin error であり、「外部取得境界で何が起きたか」
を表す。これは Stage 1 acquisition でも Stage 2 completion でも再利用されるため、
特定 stage の処理方針を class attr として持たせない。

`ExternalFetchError` が持つべき安定契約は `CODE` のみとする。

```python
class FetchRateLimitedError(ExternalFetchError):
    CODE = "fetch_rate_limited"
```

retry 可能性や失敗種別は、各 stage 側の adapter が文脈込みで projection する。

## Query Examples

retryable failure の横断集計:

```sql
SELECT stage, outcome_code, count(*)
FROM pipeline_events
WHERE event_type = 'failed'
  AND retryability = 'retryable'
GROUP BY stage, outcome_code;
```

記事削除を伴う curation failure:

```sql
SELECT id, occurred_at, article_id, outcome_code
FROM pipeline_events
WHERE stage = 'curation'
  AND event_type = 'failed'
  AND payload->>'failure_action' = 'drop_article';
```

stage-local failure kind の集計:

```sql
SELECT stage, payload->>'failure_kind' AS failure_kind, count(*)
FROM pipeline_events
WHERE event_type = 'failed'
GROUP BY stage, payload->>'failure_kind';
```

completion の claim 試行番号を見る:

```sql
SELECT id, occurred_at, outcome_code, payload->>'attempt_count' AS attempt_count
FROM pipeline_events
WHERE stage = 'completion'
  AND payload ? 'attempt_count';
```

## Non-goals

- `failure_kind` を全 stage 共通 enum にしない。
- `failure_action` に `keep_article` / `keep_curation` を入れない。
- completion 以外に `attempt_count` / `retry_attempt` payload を増やさない。
- `error_class` FQN を主契約にしない。
