# LLM Recorder 観測集約 slice 仕様

更新日: 2026-08-30

実装状況: Usage 所有権の移動済み。metric の `result` を外し、終わり方は
`status`、失敗の中身は `failure_code`（想定外は閉じた `unclassified`）。
provider span の `result` 桶も外し、終わり方は span 属性 `status`、失敗の中身は
`error.type`（= `failure_code`）。Recorder は context manager。span は Recorder が開く。
成功は context の正常終了。`LlmAttemptFailed` は Runtime が所有する。
`LlmAttemptSucceeded` と `LlmCall` / `start`/`end` は削除済み。
`PhaseCall` / `ToolCall` は工程 Recorder とは別判断で残す。

## 位置付け

工程 Recorder（Planning / EvidenceCollection / InternalSearch / ExternalSearch /
EvidenceReview / DirectAnswer / EvidenceAnswer）は、Service が型付き結論を
`report_outcome()` し、Recorder が span・duration・outcome を完結させる形へ揃えた。

LLM 呼び出しの観測は工程 Recorder と同じ形になった。Runtime は結論と
`Usage` だけを渡し、`LogfireLlmCallRecorder` が `agent_provider_call` の開閉と
metric を完結させる。

ExternalSearch の詳細な `failure_code` 追加は別 slice とする。複数 query が異なる理由で
失敗しうるため、単一 code への集約規則が先に必要だからである。

## Work Definition

### Problem

provider attempt の観測が Runtime 実装に複製されており、provider 固有処理と記録手順が
同じ関数に混在している。新しい provider Runtime を足すとき、呼び出し本体だけでなく
span lifecycle・result 写像・usage 属性書き込みもコピーする必要がある。逆に記録契約を
変えるときも、Gemini と DeepSeek の両方を同時に直す必要がある。

本 slice は SDK usage 写像を Runtime へ、span 開閉と metric を Recorder の
context manager へ分け、成功型をたたんでこの混在を解消する。

### Evidence

- 呼び出し境界は既に分かれている。公開 `AgentRuntime` / `StreamingAgentRuntime` は
  provider 中立で、具象は `GeminiAgentRuntime` と `DeepSeekAgentRuntime`。
  例外翻訳も `translate_gemini_error` / `translate_deepseek_error` に分離済み。
- 両 Runtime は `async with LlmCallRecorder.record(...)` で attempt を囲む。
  描画・request 構築の失敗では record しない。span は Recorder が `mode` で開く。
- Gemini stream は `mode="stream"`。Recorder が
  `_TRACER.start_span(..., context=parent_context)` の detached span を開き、
  途中停止時も見た usage を残す。
- Recorder は `Usage` と失敗（分類済みまたは `unclassified`）だけを受け取る。
  SDK usage 欄名の写像は各 Runtime。
- 失敗の正本は Runtime の `LlmAttemptFailed(failure_code)`。想定できる失敗は
  `CODE` / `defect`、想定外だが provider 呼び出し中の失敗は `unclassified`。
  成功型は持たない。成功は context の例外なし終了（結果を返したこと）。
  span に `result` 桶は付けない。終わり方は `status`（`PhaseStatus`）、失敗の中身は
  `error.type`（= `failure_code`）。`report_outcome` は失敗型だけを受ける。
- `attempt_number` は LLM metric 属性に載せる。
- `PhaseCall` / `ToolCall` は `recording/types.py` と export / 型テスト以外に利用者がいない。
- `record_ai_provider_exhausted` は両 Runtime が分類済み枯渇を直接 emit しており、
  本 slice では動かさない。

### Invariants

- 公開 `AgentRuntime` / `StreamingAgentRuntime` Protocol、retry、provider 例外翻訳を
  変更しない。
- 記録単位は provider attempt 1回。描画・request 構築の失敗では span も metric も作らない。
- Gemini・DeepSeek 固有処理（request 形、finish_reason、block、function calling、
  SDK usage 欄、GenAI identity の値）は各 Runtime が吸収する。
- Runtime が結論と `failure_code` を決め、Recorder はそれを span・duration・outcome・
  token metric へ変換するだけにする。
- Recorder は非同期 context manager。通常 call と streaming を同じ Protocol で扱い、
  `mode` で span lifecycle だけを分ける。
- streaming の detached span と、途中停止時の usage 保持を維持する。
- 成功と報告済み失敗（分類済み / `unclassified`）を outcome counter へ記録する。
  停止と、Runtime が報告していない例外は打たない。
- duration は未分類・停止・報告なし例外を含む全 attempt を記録する。
- 失敗には `failure_code` を付け、span では `error.type` へ写す。例外クラス名は
  `failure_code` にしない。
- `attempt_number` を LLM metric へ追加する。
- metric 名（`vector.agent.llm_call.outcome` / `.duration` / `.tokens`）は維持する。
  metric 属性に `result` は付けない。終わり方は `status`、失敗の中身は `failure_code`。
  span にも `result` は付けない。終わり方は span 属性 `status`、失敗の中身は
  `error.type`。
- span・clock・outcome / duration / tokens の各 metric 障害は独立して止め、本処理へ出さない。
- prompt、入力、応答本文、例外自由文を span・metric に載せない。
- 分類済み失敗の span に exception event を付けない。`unclassified` は exception event
  を残し、`error.type=unclassified` にする。報告のない例外は Recorder が
  `unclassified` を捏造せず、`error.type` も付けない。

### Non-goals

- ExternalSearch の詳細 `failure_code` 追加、query 失敗の集約規則。
- `record_ai_provider_exhausted` の移動。
- 新しい LLM provider の追加、DeepSeek streaming、Agent 宣言や retry policy の変更。
- Live events、structured logs、非同期 export、Logfire の `gen_ai.response.model`
  自動補完の変更。
- Tavily HTTP span、工程 Recorder、embedding 呼び出しの移管。
- 工程の `PlanningSucceeded` など、空ではない成功型を同じ PR でたたむこと。

### Done

- 両 Runtime は LLM Recorder の context manager と `report_usage` / `report_outcome`
  だけを使い、span を直接開かない。
- Recorder は provider SDK の usage 形を知らない。
- 既存の provider-attempt span 契約、streaming detached span、途中停止時 usage、
  例外同一性、情報非露出がテストで確認される。
- provider span に `result` は無い。終わり方は `status`、失敗の中身は `error.type`。
- `LlmCall` と `start`/`end` API を削除する。`PhaseCall` / `ToolCall` は残す。

## 現状の境界

LLM 呼び出し本体は Runtime 単位で分かれている。

| 関心事 | 所有者 |
|---|---|
| 公開 attempt 境界 | `AgentRuntime.call` / `StreamingAgentRuntime.stream_text` |
| Gemini I/O・block・finish_reason | `GeminiAgentRuntime` |
| DeepSeek I/O・function calling | `DeepSeekAgentRuntime` |
| SDK 例外翻訳 | `translate_gemini_error` / `translate_deepseek_error` |
| 分類済み失敗型 | Runtime の `LlmAttemptFailed` |
| 枯渇 EMF | 各 Runtime 内の `record_ai_provider_exhausted` |
| span・duration・outcome・tokens | `LogfireLlmCallRecorder` |

```text
Runtime.call / _stream_fragments
  ├─ provider 固有: request, SDK call, 例外翻訳, 結論分類, SDK usage → Usage
  ├─ provider 固有: GenAI identity を Recorder へ渡す
  └─ async with record(...) / report_usage / report_outcome(失敗)

LlmCallRecorder
  ├─ mode=call: 現行 context の子として agent_provider_call を開く
  ├─ mode=stream: 渡された parent_context から detached span を開く
  └─ 抜け方 → span status / error.type / duration / outcome / tokens
```

成功は結果を返すことなので、Recorder は例外なしで抜けたことを成功とみなす。
新しい provider は Runtime を足す。記録契約の変更は Recorder だけを直す。

## Recorder contract

### 記録結論

```text
（成功型はない。例外なしで context を抜けたことが成功）
LlmAttemptFailed(failure_code)   # Runtime が所有する
```

- 想定できる失敗は `CODE` / `defect`。想定外だが provider 呼び出し中の失敗は
  閉じた `unclassified`。例外クラス名は `failure_code` にしない。
- span に `result` は付けない。終わり方は `status`、失敗の中身は `error.type`。
- `failure_code` は空文字を拒否する。成功・停止にはキーも `"none"` も付けない。
  duration / tokens にも付けない。
- 停止は結論型を作らない。Recorder が cancel / GeneratorExit から status を決める。
- Runtime が報告していない例外は欠陥経路とし、Recorder は `unclassified` を捏造しない。
- `report_outcome` は失敗だけを受ける。成功型は作らない。

### Recording handle

```text
LlmCallRecording
  report_usage(usage)
  report_outcome(failure)
```

- `report_usage` は何回呼んでもよい。stream は chunk ごとに上書きし、最後に存在した
  `Usage` を採用する。欠損欄は `None` のままにし、0 で埋めない。
- 途中停止でも、それまでに報告した usage は残す。
- `report_outcome` は失敗のとき呼ぶ。成功は呼ばない。失敗型だけを受ける。

### Recorder Protocol

```text
LlmCallRecorder.record(
  *,
  agent_name,
  provider,          # metric 用。agent.model.provider（"gemini" / "deepseek"）
  model,
  attempt_number,
  prompt_version,
  operation_name,    # gen_ai.operation.name。Runtime が渡す
  gen_ai_provider,   # gen_ai.provider.name。Runtime が渡す（"gcp.gemini" / "deepseek"）
  mode,              # "call" | "stream"
  parent_context,    # stream のみ。stream_text 呼び出し時点の context
) -> AbstractAsyncContextManager[LlmCallRecording]
```

`provider` と `gen_ai_provider` を混用しない。metric の `provider` は
`ModelTarget.provider`、span の `gen_ai.provider.name` は Runtime が決める
OTel 値である。Recorder は `"gemini"` から `"gcp.gemini"` を推測しない。

### mode と span lifecycle

- `call`: 現行の `logfire.span("agent_provider_call", CLIENT, ...)` 相当。
  呼び出し中の OTel context の子になる。
- `stream`: 現行の `_TRACER.start_span(..., context=parent_context)` 相当。
  consumer 側の context が変わっても、`stream_text` 開始時点の親の子として残す。

どちらも span 名 `agent_provider_call`、SpanKind `CLIENT` は変えない。

### 終了分類

| 終わり方 | status | outcome counter | duration | span `status` | `error.type` | OTel | exception event |
|---|---|---|---|---|---|---|---|
| 例外なし（結果を返した） | completed | 打つ | 記録する | completed | 付けない | UNSET | なし |
| 分類できた失敗を報告して raise | failed | `failure_code` 付き | 記録する（failure_code なし） | failed | CODE / defect | ERROR | なし |
| `unclassified` を報告して raise | failed | `failure_code=unclassified` | 記録する（failure_code なし） | failed | unclassified | ERROR | あり（秘匿） |
| 停止（cancel / GeneratorExit / stream aclose） | stopped | 打たない | 記録する | stopped | 付けない | UNSET | なし |
| 報告なしの例外（欠陥経路） | failed | 打たない | 記録する | failed | 付けない | ERROR | あり。`unclassified` を捏造しない |

clock が取れなかった attempt は duration を打たない。usage が無い attempt は
token metric を打たない。

### metric 属性

既存名を維持し、属性は次を正本にする。

```text
vector.agent.llm_call.outcome
  agent_name, provider, model, attempt_number, status
  失敗のみ failure_code（CODE / defect / unclassified）

vector.agent.llm_call.duration
  agent_name, provider, model, attempt_number, status

vector.agent.llm_call.tokens
  上記 + direction: input | output | cache_read_input | reasoning_output
```

`attempt_number` は今回追加する。metric の `result`（`succeeded` / `failed` /
`none`）は付けない。終わり方は `status`、失敗の中身は outcome の `failure_code`。

### span 属性

独自 allowlist は `agent_name` / `attempt_number` / `prompt_version` / `status`。
`error.type` は標準キー。`result` は付けない。

GenAI 標準属性は Runtime が渡した identity と、報告された `Usage` から Recorder が書く。

- `gen_ai.operation.name`
- `gen_ai.provider.name`
- `gen_ai.request.model`
- 存在する usage だけ `gen_ai.usage.*`（input / output / cache_read.input /
  reasoning.output）。無い欄は 0 で補わない。

`gen_ai.response.model` は Runtime / Recorder が明示設定しない。

## Runtime の残す責務

各 Runtime は次だけを行う。

1. 正の `attempt_number` と provider 一致を検証する。
2. 入力描画と request 構築を記録開始前に終える。
3. Recorder を `mode` 付きで開き、SDK を呼ぶ。
4. SDK usage を `Usage` へ写して `report_usage` する。
5. 失敗なら `report_outcome(LlmAttemptFailed)` してから同じ例外を raise する。
   想定外は `failure_code=unclassified`。
6. 枯渇系だけ `record_ai_provider_exhausted` を呼ぶ（`unclassified` では呼ばない）。

Gemini 固有の finish_reason / prompt block / stream 切断、DeepSeek 固有の
declared function 欠落は、これまでどおり各 Runtime が結論へ写す。span の桶へ
畳まない。call と stream の結論分類そのものは本 slice で変えない。

SDK usage 欄名の写像は各 Runtime が行い、Recorder は `Usage` だけを受け取る。

## 削除対象

削除済み。

- `LlmCall`（start ハンドル。context manager に置換）
- `LlmAttemptSucceeded`
- `LlmCallResult`（span の result 語彙。attempt の集計は `PhaseStatus`）
- `LlmCallRecorder.start` / `end`
- 両 Runtime の `_record_classified_error` / `_record_usage` と直接の
  `logfire.span` / `_TRACER.start_span`

`Usage` と `PhaseStatus` は残す。`PhaseCall` / `ToolCall` は
工程 Recorder の成功型たたみとは別判断のため、本 slice では残す。

## 実装順

1. SDK usage 欄名の写像を各 Runtime へ移し、Recorder は `Usage` だけを受け取る。
2. outcome を分類済み失敗の metric `result=failed` へ畳み、outcome にだけ
   `failure_code` を付ける。`end(result=)` と `outcome_from_span_result` を削除する。
3. `LlmAttemptFailed` を Runtime へ移す。
4. Recorder を context manager にし、正常終了を成功とみなして `LlmAttemptSucceeded`
   を削除する。span / clock / 各 metric の独立障害、classified-only outcome、
   duration の全 attempt、`attempt_number`、`mode` による span lifecycle を固定する。
5. Gemini `call` / `stream_text` と DeepSeek `call` を同じ Protocol へ移す。
6. 死んだ `LlmCall` と `start`/`end` を削除する。
7. metric の `result` を外し、想定外の provider 呼び出し失敗は Runtime が
   `unclassified` を報告する。Recorder は報告のない例外を捏造しない。
8. span の `result` 桶を外す。終わり方は span 属性 `status`、失敗の中身は
   `error.type`。`report_outcome` は失敗型だけを受ける。

## Verification policy

テストは公開された attempt 結果、span の意味的な属性、metric 属性、例外同一性、
本文非露出を検証する。内部 helper の個数や、特定の `try/finally` 形状は固定しない。

既存の正本テストを維持して契約の移動を確認する。

- `tests/agent/runtime/test_gemini_tracing.py`
- `tests/agent/runtime/test_deepseek_tracing.py`
- `tests/agent/runtime/test_streaming_contract.py`
- `tests/agent/runtime/test_gemini_llm_call_recording.py`
- `tests/agent/runtime/test_deepseek_llm_call_recording.py`
- `tests/agent/runtime/test_streaming_llm_call_recording.py`

Recorder 単体は `tests/agent/recording/test_llm_recorder.py` を context manager 契約へ
書き換える。SDK usage 欄の写像は Runtime tracing / recording テストが正本で、
`Usage` 構築の全欠損 / bool 除外は `tests/agent/recording/test_types.py` が担う。
