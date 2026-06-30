# Question Planner Audit Spec

Status: Draft
Created: 2026-06-29
Scope: question planner の失敗分類・監査・メトリクス設計

## Problem

Question planner は LLM 境界であり、plan 作成時に provider 障害、schema 不一致、JSON 不正、retry 後の fallback などが起きる。ユーザーリクエスト自体は fallback で継続できても、planner としてどこが失敗したのかを後から確認できる必要がある。

この仕様では planner attempt ごとの失敗と、最終的な plan / fallback 結果の監査方針を定義する。

## Decisions

- AI分析 stage の `Recoverable` / `Terminal` という語彙は planner 監査では使わない。
- Planner は pipeline stage ではなく同期ユーザーリクエスト内の処理なので、分類軸は「この request 内で retry するか」に寄せる。
- 時間を空ければ回復しうるかは planner 独自分類にはしない。既存 provider の `failure_kind` に残せば十分。
- 失敗は attempt ごとに audit へ焼く。
- 失敗 audit は service 全体の transaction にまとめず、best-effort の別 transaction でよい。
- 最終的に plan が作れたか、fallback を使ったかは別 event として焼く。
- raw question / raw prompt / raw response は監査に保存しない。
- メトリクスは低 cardinality な outcome 集計に限定する。
- AI 処理と外部 provider 接続で共通する既存部品は import して使い、二重定義しない。
- Pipeline stage 固有の型・語彙・制御フローは planner に import しない。

## Dependency Boundary

Planner は agent 配下の機能だが、AI provider error や Gemini 接続のような共通部品は既存実装を再利用する。

Import してよいもの:

- AI provider error / failure translator など、provider 境界で共通に使える型・関数。
- Gemini client / request helper など、外部 provider 接続に関する共通部品。
- Provider の `failure_kind`, `failure_reason`, `code` を取り出すための薄い utility。

Import しないもの:

- analysis pipeline の stage 実行制御。
- stage 固有の `Recoverable` / `Terminal` 語彙。
- stage 固有の retry / backoff / enqueue / job lifecycle。
- analysis stage 専用 payload を planner audit payload として流用すること。

この境界により、Gemini や AI error の分類は重複させず、agent planner の監査語彙は request-local なものに保つ。

## Request Retry Disposition

Planner 固有の retry 判断は request-local な語彙にする。

```python
class RequestRetryDisposition(StrEnum):
    RETRY_IN_REQUEST = "retry_in_request"
    DO_NOT_RETRY_IN_REQUEST = "do_not_retry_in_request"
    UNKNOWN = "unknown"
```

`RETRY_IN_REQUEST` は同じユーザーリクエスト内で短く再試行する価値がある失敗を表す。

`DO_NOT_RETRY_IN_REQUEST` は同じユーザーリクエスト内で再試行しない失敗を表す。rate limit や service unavailable は時間経過で回復しうるが、リアルタイム応答では待たず fallback / insufficient に倒す。

`UNKNOWN` は分類不能な想定外失敗に使う。

## Failure Classification

Provider 由来の失敗は既存の `AIProviderError` から planner 用 failure attributes へ写す。

```text
failure_kind = exc.FAILURE_MODE.value
failure_reason = exc.reason.value | None
code = exc.CODE
request_retry_disposition = do_not_retry_in_request
```

Planner response shape 由来の失敗は provider 由来ではないため、次のように扱う。

```text
failure_kind = ai_response_invalid
failure_reason = defect.value
request_retry_disposition = retry_in_request
```

対象例:

- `question_planner_response_gemini_not_json`
- `question_planner_response_gemini_not_object`
- `question_planner_response_pydantic_validation_failed`

想定外失敗は次のように扱う。

```text
failure_kind = unknown
failure_reason = None
request_retry_disposition = unknown
code = unexpected_error
```

## Audit Events

Planner audit は attempt-level event と final-result event を分ける。

### Attempt Failure

planner 呼び出し 1 回が失敗するたびに焼く。

```text
event_type = failed
outcome_code = question_plan_attempt_failed
```

Payload:

```python
class AgentPlannerPayload(BasePipelineEventPayload):
    kind: Literal["agent_planner"] = "agent_planner"
    attempt_number: int | None = None
    attempt_count: int | None = None
    retry_used: bool | None = None
    fallback_used: bool | None = None
    request_retry_disposition: str | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None
    retrieval_mode: str | None = None
    internal_query_count: int | None = None
    external_query_count: int | None = None
    ai_model: str | None = None
    prompt_version: str | None = None
```

Attempt failure では `retrieval_mode` / query count は通常 `None` でよい。

### Plan Created

最終的に planner が plan を返せた場合に焼く。

```text
event_type = succeeded
outcome_code = question_plan_created
```

1回目失敗後の retry で成功した場合も同じ outcome_code にし、payload の `attempt_count` / `retry_used` で区別する。

Payload:

```text
attempt_count = 1 | 2
retry_used = false | true
fallback_used = false
retrieval_mode = final plan retrieval_mode
internal_query_count = len(final internal_queries)
external_query_count = len(final external_queries)
ai_model = planner model
prompt_version = planner prompt version
```

### Fallback Used

retry 後も planner が plan を作れず、safe fallback plan を使った場合に焼く。

```text
event_type = failed
outcome_code = question_plan_fallback_used
```

Payload:

```text
attempt_count = actual planner call count
retry_used = true | false
fallback_used = true
retrieval_mode = fallback plan retrieval_mode
internal_query_count = len(fallback internal_queries)
external_query_count = len(fallback external_queries)
failure_kind = final failure family
failure_reason = final failure reason
request_retry_disposition = final failure disposition
```

Fallback は agent 全体としては処理継続できるが、planner としては失敗なので `failed` として記録する。
Provider error のように `request_retry_disposition=do_not_retry_in_request` で retry しない場合は、`attempt_count=1` / `retry_used=false` の fallback event になりうる。

## Transaction Boundary

Planner audit は業務データ更新と同一 transaction にまとめない。

- planner 失敗は attempt ごとに best-effort 別 transaction で記録する。
- audit 書き込みに失敗しても planner / agent の処理は止めない。
- audit drop は既存の `vector.audit.dropped` 相当で別途集計する。
- 将来 request 全体の trace / correlation id を持つ場合は、各 planner event を同じ request correlation に紐づける。

この方針により、1回目失敗、2回目失敗、fallback 使用のような連続事象を欠落なく追いやすくする。

## Metrics

Metric は監査の代替ではなく集計専用にする。
Planner metric は「最終的に plan を作れたか」を 1 request につき 1 回だけ記録する。attempt ごとの失敗や retry 開始は audit event で追う。

```text
vector.agent.planner.outcome
```

Attributes:

```text
result = planned | fallback | failed
retry_used = true | false
planned_retrieval_mode = none | internal | external | internal_and_external | unknown
```

`planned` は planner が `QuestionPlan` を返せたことを表す。1回目で成功しても、1回目失敗後の retry で成功しても `result=planned` で、retry の有無は `retry_used` で区別する。

`fallback` は planner が plan を返せず、safe fallback plan を使って request を継続したことを表す。

`failed` は fallback も使えず、planner 処理として request を継続できなかった場合だけに使う。`plan_question` の DB-free slice では未知例外を握り潰さず伝播させるため、`failed` は将来の agent orchestration 終端で emit する。

`planned_retrieval_mode` は planner または fallback plan が「必要だと判断した検索先」であり、実際に internal retrieval / external search が起動した事実ではない。実行事実は各 component の metric で記録する。

Internal retrieval が実際に呼ばれたことは、別 metric で記録する。

```text
vector.agent.internal_retrieval.outcome
```

Attributes:

```text
result = succeeded | empty | failed
query_count = 1 | 2 | 3
```

この metric は internal retrieval を呼び出した場合だけ emit する。呼ばなかった request は planner / agent orchestration 側の `planned_retrieval_mode` または将来の execution summary で判断する。

External search も同じ考え方で、実装時に別 metric を定義する。

```text
vector.agent.external_search.outcome
```

Attributes:

```text
result = succeeded | empty | failed
query_count = 1 | 2 | 3
```

`failure_reason`, `failure_kind`, raw question, prompt, response, query text, user id, request id は metric label に入れない。

## Persistence Notes

既存 `pipeline_events.stage` は DB CHECK 制約を持つため、`agent_planner` を pipeline_events に追加する場合は Alembic migration が必要である。

実装時に追加する候補:

- `Stage.AGENT_PLANNER = "agent_planner"`
- `AgentPlannerPayload`
- `AgentPlannerAuditRepository`
- `record_agent_planner_outcome`

DB migration を避ける段階では、まず planner error 型と mapper、metrics のみを実装し、監査永続化は後続に分離してもよい。

## Non-goals

- raw question / raw prompt / raw response の永続化はしない。
- planner 監査で pipeline stage 用の `Recoverable` / `Terminal` 語彙を使わない。
- AI provider error や外部接続 helper を agent 用に二重定義しない。
- analysis pipeline の stage 固有実装を agent planner に移植しない。
- provider failure に対して同一 request 内で待機 retry を増やさない。
- この仕様では DB migration を実装しない。

## Test Plan

- provider error が `failure_kind`, `failure_reason`, `code`, `request_retry_disposition=do_not_retry_in_request` に写る。
- response invalid が `failure_kind=ai_response_invalid`, `request_retry_disposition=retry_in_request` に写る。
- attempt failure が attempt ごとに audit repository へ渡される。
- retry 成功時は `question_plan_created` with `attempt_count=2`, `retry_used=true` になる。
- fallback 使用時は `question_plan_fallback_used` with `fallback_used=true` になり、実際の `attempt_count` / `retry_used` を記録する。
- planner outcome metric は `planned | fallback | failed` と `retry_used` を 1 request 1 回 emit する。
- DB-free `plan_question` slice では未知例外を伝播し、planner outcome metric は emit しない。`failed` は将来の orchestration 境界で扱う。
- internal retrieval metric は実際に internal retrieval が呼ばれた場合だけ emit する。
- raw question / prompt / response が payload と metrics に入らない。
- provider 共通 error を import して planner failure attributes に写せる。
- stage 固有語彙が planner の public 型・payload・metrics に混入しない。
