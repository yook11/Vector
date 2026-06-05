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

- `source_dispatched`
- `source_not_registered`
- `curated_signal`
- `assessed_out_of_scope`
- `briefing_generation_input_empty`
- `stale_attempt`

dispatch stage は fan-out 入口のため、source 単位 outcome と run 単位 outcome を
分ける。

- source 単位: `source_dispatched` / `source_enqueue_failed` /
  `source_not_registered` / `source_name_invalid`
- run 単位: `dispatch_run_no_targets` / `dispatch_run_failed`

backfill stage は、通常 task の成功/失敗ではなく「救済投入できたか」を焼く。

- item 単位: `backfill_item_enqueued` / `backfill_item_enqueue_failed`
- run 単位: `backfill_run_no_targets` /
  `backfill_run_kill_switch_disabled` /
  `backfill_run_held_by_stage_hold` /
  `backfill_run_daily_budget_exhausted` /
  `backfill_run_completed` / `backfill_run_failed`
- 救済断念: `backfill_curation_aged_out` /
  `backfill_assessment_aged_out` / `backfill_embedding_aged_out`

backfill run / item event の payload は `kind="backfill"` とし、`run_id`、
`backfill_stage`、`target_kind`、`target_id`、件数 snapshot
(`selected_count` / `granted_count` / `enqueued_count` / `failed_count`)、
`limit`、`daily_max` を必要に応じて保存する。skip 理由は `outcome_code` を
SSoT とし、payload に別の reason field は持たせない。

通常 AI stage (`curation` / `assessment` / `embedding`) の task 入口では、
Ready 構築が業務状態により進めなかった場合を rejected として焼く。
blocked 理由は `ReadyFor*` の domain gatekeeper が判定し、repository は
DB 事実取得のみを担う。

- curation: `curation_ready_build_blocked_article_missing` /
  `curation_ready_build_blocked_already_curated` /
  `curation_ready_build_blocked_already_rejected_as_noise` /
  `curation_ready_build_blocked_content_too_large`
- assessment: `assessment_ready_build_blocked_curation_missing` /
  `assessment_ready_build_blocked_already_in_scope` /
  `assessment_ready_build_blocked_already_out_of_scope`
- embedding: `embedding_ready_build_blocked_analysis_missing` /
  `embedding_ready_build_blocked_already_embedded`

Ready 判定中の例外は failed として焼き、task は raise する。

- `*_ready_build_failed_db_error`
- `*_ready_build_failed_contract_invalid`
- `*_ready_build_failed_unexpected_error`

Ready build blocked / failed では `outcome_code` を SSoT とし、payload に
`skip_reason` や blocked code は重複保存しない。blocked は trigger ID
(`target_article_id` / `curation_id` / `analysis_id`) を payload に残し、
top-level `article_id` は保存しない。curation の content too large だけ、
判定時の evidence として `input_content_length` / `max_content_length` を残す。
通常 AI stage の blocked outcome は `*ReadyBuildBlockedCode.value` をそのまま
焼く。

completion stage の `scrape_html_body` 入口でも、pending から Ready を構築
できなかった結果を audit する。pending lifecycle による対象外は skipped とし、
構築中の例外は failed として焼く。

- skipped: `completion_ready_build_blocked_pending_missing` /
  `completion_ready_build_blocked_pending_not_running`
- failed: `completion_ready_build_failed_db_error` /
  `completion_ready_build_failed_observed_article_invalid` /
  `completion_ready_build_failed_source_not_registered` /
  `completion_ready_build_failed_url_invalid` /
  `completion_ready_build_failed_unexpected_error`

completion Ready build event の payload は `kind="completion"` とし、
`pending_id`、`pending_status`、`canonical_url`、`attempt_count`、
`source_name` を解決できた範囲で保存する。`staged_attributes` の中身は
payload に保存しない。pending lifecycle / URL の completion typed error は
`CODE` / `EVENT_TYPE` / `FAILURE_KIND` を SSoT とする。`ObservedArticle` 復元失敗は
`ObservedArticleInvalidError`、source registry miss は `SourceNotRegisteredError`
をそのまま伝え、audit 側で completion Ready build outcome に投影する。
`source_not_registered` は dispatch / completion で同じ registry miss 概念だが、
dispatch では outcome、completion では failure_kind として使う。acquisition の
raw `SOURCES[...]` miss は trusted dispatch invariant 違反として現状維持し、
completion registry helper の miss は永続 pending 由来の実行時 failure として
audit する。
監査 payload 用の `pending_id` / `attempt_count` などは error には詰めず、task /
audit 側が task 引数と pending facts から補う。task は completion typed error の
`EVENT_TYPE` が `failed` なら audit 後に raise し、`skipped` なら audit 後に
return する。DB 例外と想定外例外だけ audit 側で fallback 分類する。

trend discovery stage は、保存物としての snapshot ではなく、rolling 7d の
分析済み記事から trend discovery run がどう終わったかを焼く。

- succeeded: `trend_discovery_run_completed` /
  `trend_discovery_run_updated`
- skipped: `trend_discovery_run_no_target_articles` /
  `trend_discovery_run_already_exists` / `trend_discovery_run_conflict`
- failed: `trend_discovery_run_failed`

trend discovery event の payload は `kind="trend_discovery"` とし、
`window_start`、`window_end`、`trigger` (`cron` / `cli`)、
`requested_update`、`source_analysis_count`、`completed_category_count` を保存する。
カテゴリ集計前に skip / failure した場合、`completed_category_count` は `null`。
failed event の `retryability` は `unknown` とする。

briefing stage は、カテゴリ task を積む dispatch と 1 カテゴリ分を生成する
generation に分けて焼く。

- dispatch summary: `briefing_dispatch_completed`
- category enqueue: `briefing_category_enqueued` /
  `briefing_category_enqueue_failed`
- dispatch failure: `briefing_dispatch_category_master_load_failed`
- generation: `briefing_generation_completed` /
  `briefing_generation_already_exists` /
  `briefing_generation_input_empty` /
  `briefing_generation_llm_configuration_invalid` /
  `briefing_generation_llm_provider_call_failed` /
  `briefing_generation_llm_response_contract_invalid`

briefing dispatch summary の payload は `kind="briefing"` とし、`week_start`、
`selected_category_count`、`enqueued_category_count`、
`failed_category_count` を保存する。カテゴリ単位 event は `week_start`、
`category_id`、`category_slug` を保存する。`run_id` は持たせず、
`week_start + category_id` を追跡キーにする。

## Backfill Exclusion Events

Stage 4/5 の backfill age-out は、Stage 3 の物理削除とは非対称に扱う。
Stage 4 は `ArticleCuration`、Stage 5 は `InScopeAssessment` という保全価値のある
部分結果を持つため、記事や分析結果を削除せず current-state sentinel によって
通常 backfill から除外する。

- 現在状態: `assessment_backfill_exclusions` /
  `embedding_backfill_exclusions`
- 監査履歴: `pipeline_events` の `backfill_assess` /
  `backfill_embed` rejected event
- outcome code: `BackfillExclusionReason` enum value を SSoT とする

`pipeline_events` は immutable audit であり、通常 backfill の制御状態は
`*_backfill_exclusions` 側で保持する。

### retryability

同一入力を将来再実行したときに回復しうるか、という横断軸。

値:

- `retryable`
- `non_retryable`
- `unknown`

retry 上限に到達した事実は intrinsic な retryability ではないため、必要な stage は
`payload.retry_exhausted` で表す。

### failure_kind

失敗の原因ファミリー。処理方針 (retry / hold) ではなく「何が起きたか」の括りを表す。

例:

- curation: `terminal_drop` / `terminal_keep` / `recoverable`
- assessment / embedding: provider 由来は AI provider の回復クラス
  (`AIProviderFailureMode`) の値 — `attempt_scoped` / `time_based_recovery` /
  `condition_based_recovery` / `operator_action_required` / `target_rejected`。
  応答が分析に使えない parse 由来は `ai_response_invalid`
  (retry / hold は failure_kind ではなく回復クラス由来の `retryability` 軸と
  handler の hold 導出が担う)
- briefing: `configuration` / `response_invalid` / `llm_error`
- DB adapter: `db_runtime` / `db_constraint` / `db_query_or_schema` / `db_unknown`

### failure_reason

原因の詳細。`failure_kind` (ファミリー) / `outcome_code` (具体 CODE) とは別軸で、
provider error が検知箇所で名乗る理由ラベル (`AIProviderStateError.reason` /
`AIProviderContentError.reason`、PII-free な `StrEnum` value) を運ぶ。

- assessment / embedding の provider 由来失敗で焼かれる
  (例: `timeout` / `server_error` / `safety` / `context_length`)
- provider 由来でない失敗 (parse / DB / catch-all)、reason 未指定の state error は
  `None`
- payload 任意フィールドのため migration 不要 (acquisition の `fetch_reason` /
  `read_format` と同列)

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

assessment / embedding の Layer 1 marker (`*RecoverableError` / `*TerminalError`)
は例外で、原因軸 (`failure_kind` / `failure_reason`) を **instance 属性** として持つ
(型で固定するのは retry 軸 `RETRYABILITY` のみ)。ACL の mapper が provider error の
回復クラス `FAILURE_MODE` から `failure_kind = mode.value` を、`reason` から
`failure_reason` を導出して詰める。hold (stage 退避) は marker 型ではなく handler が
`provider_error.FAILURE_MODE` から導出する。projection helper は `failure_kind` を
instance 値優先で読み、curation / briefing / completion / acquisition の classvar
`FAILURE_KIND` に fallback する。

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
