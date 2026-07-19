# Direct / Evidence Answer Agent と streaming runtime slice 仕様

更新日: 2026-07-19

実装状況: Implemented（PR7）

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md`のPR7を具体化する。

最後の2つのGemini役割(Direct Answer / Evidence Answer)を共通`Agent`宣言へ移し、その最初の実consumer
として、既存`AgentRuntime`とは分けた`StreamingAgentRuntime` capabilityを追加する。既存メソッド
`AgentRuntime.invoke`の命名と呼び出し向きは本sliceで変えず、streaming版は暫定的に
`StreamingAgentRuntime.invoke_stream`とする。Agentをstatic declarationと動作主体のどちらとして読むか、
および最終的なattempt API名は、migration完了後の命名整理でまとめて再訪する。

flow(`DirectAnswerFlow` / `EvidenceAnswerFlow`)はphase policy ownerとして残り、attempt loop、
continuation、delta配信、最終parse / finalize、fallbackを引き続き所有する。本sliceが動かすのは
provider機構(stream呼び出し・分類・cleanup)と宣言の正本だけである。

前提: PR1〜PR6(共用scope factory `activate_gemini_agent_runtime`を含む)。本slice完了で
Gemini 4役割すべてが共通runtimeとfactoryに乗り、回答Run全体のGemini resource scope
(親仕様「PR7以降」)の前提が揃う。

## Work Definition

### Problem

- `GeminiDirectAnswerGenerator` / `GeminiEvidenceAnswerDraftGenerator`が役割宣言、prompt構築、
  client、streaming呼び出し、provider分類をまとめて持つ「全部入りadapter」のまま残っている。
- `AgentRuntime`にstreaming契約が無く、answer系だけが共通runtime / scope factory / span契約の
  外にある。
- 両roleのmodel / prompt / versionが旧call specに集約され、共通Agent宣言から読めない。

### Evidence

- `direct_answer/flow.py`: 2 attempt loop、`previous_error = str(exc)`、retryは
  `classify_direct_answer_failure()`の`RETRY_IN_REQUEST`判定のみ。非retriableは
  metric記録後に**raise**(fallbackなし)。fragment間で
  `ensure_answer_generation_continues()`を確認し、`LiveAnswerDraftSession(generation=attempt)`
  でdeltaを配信する。最終処理はflow側: fragment結合、citation marker除去、空文字なら
  `DirectAnswerInvalidError`。flowの`finally`が`close_answer_stream()`でgeneratorを閉じる。
- `evidence_answer/flow.py`: 同じ2 attempt構造だが非retriable時は**safe fallback draft**
  (固定文言+delta reset / append / finish)を返す。streaming中は
  `IncrementalJsonAnswerExtractor`が構造化JSONから`answer`fieldを漸進デコードしてdeltaへ流し、
  終端で`parse_evidence_answer_final_json()` -> `finalize_evidence_answer_draft()`。
  **finalizeは`evidence`と`requirement_ids`というworkflowデータを必要とする**。捕捉は
  provider error / 2つのrole error / `ValidationError`。
- `direct_answer/ai/gemini.py`: async generator portが`generate_content_stream()`を呼び、
  chunkごとにprompt block検査・blocked finish reason検査を行い、text fragmentをyieldする。
  終端reasonを見ずにstreamが終わると`STREAM_TRUNCATED`の`AIProviderNetworkError`。
  未分類は`translate_gemini_error()`。自身の`finally`でSDK streamを`aclose`する
  (flow側closeとの二層cleanupが現行形)。clientは`__init__`で誕生する(=graph構築時)。
- spec実値: 両roleとも`temperature=0.2` / `max_output_tokens=2048`。**directは
  `response_schema=None`のtext stream**、evidenceはstructured output
  (`response_mime_type` + 手書きschema)のstreaming JSON。versionは旧
  `compute_call_signature()`、rate limit policyは休眠。
- evidenceのlenient raw draft契約(`cited_refs: list[object]`等)がstreaming JSONの受け皿で、
  strict化はflowのfinalizeが行う。

### Invariants

#### Streaming runtime契約

- non-streamingの`AgentRuntime.invoke`は変更しない。streamingは別capabilityである
  `StreamingAgentRuntime.invoke_stream`として追加し、DeepSeek Runtimeやnon-streaming fakeへ
  不要なメソッドを要求しない。Gemini Runtimeは両Protocolを満たす。
- streaming flowへ注入するscope factoryも`StreamingAgentRuntimeScopeFactory`として分け、戻り値が
  `invoke_stream`とclose可能なstream契約を持つことを型で保証する。

```python
class AgentRuntime(Protocol):
    async def invoke[InputT, OutputT](
        self, agent: Agent[InputT, OutputT], input: InputT, *, attempt_number: int,
    ) -> OutputT: ...


class AgentTextStream(Protocol):
    def __aiter__(self) -> AgentTextStream: ...
    async def __anext__(self) -> str: ...
    async def aclose(self) -> None: ...


class StreamingAgentRuntime(Protocol):
    def invoke_stream[InputT, OutputT](
        self, agent: Agent[InputT, OutputT], input: InputT, *, attempt_number: int,
    ) -> AgentTextStream: ...


class StreamingAgentRuntimeScopeFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[StreamingAgentRuntime]: ...
```

- 契約は意図的に非対称とする: `invoke`は検証済み`OutputT`を返し、`invoke_stream`は
  model text fragmentの列を返す。streamingの最終validation / finalizeはworkflowデータ
  (evidence、requirement_ids)を要するためruntimeへ置けない。`StreamingAgentRuntime`は`OutputT`を
  返す契約を持たず、Flowが`agent.output_type`を最終parse / draft構築の正本として使用する。
- `invoke_stream`はfragmentを加工せずyieldし、1 invocationにつきprovider streamを
  1本だけ開く。retry / fallback / delta配信 / continuation判断を行わない。
- runtimeが吸収するprovider機構(現generatorから移設): prompt block検査、blocked finish
  reason分類、終端reason欠落の`STREAM_TRUNCATED`化、`translate_gemini_error()`、
  SDK streamの`finally` `aclose`。すべて既存`AIProviderError`系語彙で、新defectを追加しない。
- renderer、provider誤配線検査、provider config構築、親OTel contextの捕捉は
  `invoke_stream()`呼び出し中に同期的に完了する。ここで失敗した場合、戻り値iterator、attempt span、
  provider streamを作らない。attempt spanとprovider streamは、返したinner async generatorが
  初めて反復されたときだけ開始する。一度も反復されず`aclose()`されたstreamはprovider call / spanとも0回。

#### Streaming attempt span(手動lifecycle)

- `invoke_stream()`呼び出し時にphaseのOTel parent contextを捕捉するが、attempt spanはまだ開始しない。
  inner async generatorの初回実行時に、公開OTel API
  (`Tracer.start_span(..., context=captured_parent)` / `Span.end()`)で
  `agent_provider_call`をdetached spanとして開始する。Logfireのprivate `_start` / `_end`は使わない。
- attempt spanをambient contextとして`yield`越しに保持しない。fragmentがconsumerへ返った直後も
  current spanはphaseのままとし、別contextからconsume / closeされてもattemptの親は生成時に捕捉した
  phaseで固定する。本sliceはSDK内部のtransport spanをattempt配下にする契約を追加しない。
- SDK streamを取得した場合はgeneratorの`finally`で`aclose()`を1回試行する。未取得なら0回とする。
  SDK closeを試行した成否にかかわらず、さらに外側の`finally`でattempt spanをちょうど1回終了する。
  現行どおり通常の`Exception`であるSDK close失敗はbest-effortとして抑止するが、
  `asyncio.CancelledError`は抑止しない。
- 分類済みprovider errorは`result` / `error.type`とdescriptionなしのERROR statusを記録し、
  SDK close、span終了の**後**に同じerrorをraiseする。translated errorの`__cause__`も維持し、
  exception eventを作らない。
- 未分類errorはexception eventを1件明示記録し、`result`を付けずERROR statusとして同じerrorを伝播する。
  event / statusの自由文は既存export redactionに委ねる。
- consumerによる途中`aclose()`(`GeneratorExit`)とtask cancellation(`asyncio.CancelledError`)は別経路として
  扱う。いずれも新しい`result` / `error.type` / exception eventを付けず、UNSET statusのままspanを閉じる。
  cancellationはprovider `__anext__()`待機中とconsumerのfragment処理中の両方を対象とする。
- usage metadataはchunkに実在するとき、本文yield・blocked判定より前に既存の`gen_ai.usage.*`規則で
  記録する。終端前の放棄で未受信なら属性を作らず、受信済みusageは後続cancelでも消さない。
- 正常な終端reasonを確認してEOFし、SDK close中にcancelされなかったprovider streamだけを
  `result="succeeded"`とする。Flowが後続でblank / JSON / grounding違反を検出しても、すでに閉じた
  provider attempt spanを`invalid_response`へ変更しない。role validation failureはphase policyのretry / fallback
  とoutcome metricで観測する。このstreaming時の意味差を意図的に受け入れる。
- streaming attempt span durationはprovider stream開始からSDK iterator closeまでで、consumerの
  backpressureによりgeneratorがyieldで停止している時間も含む。client scopeのclose時間は含めない。
- span attribute / event / status descriptionへfragment本文・prompt・evidence・回答本文を
  記録しない。deltaがユーザーへ届くのはSSE経路の設計であり、観測経路には載せない。

provider stream outcomeは次で固定する。

| 経路 | result | error.type | status | exception event |
|---|---|---|---|---|
| 正常EOF | `succeeded` | なし | UNSET | なし |
| SAFETY / RECITATION | `blocked` | 既存provider code | ERROR、descriptionなし | なし |
| prompt block / translated provider error / truncated | `provider_error` | 既存provider code | ERROR、descriptionなし | なし |
| 未分類error | なし | なし | ERROR | 1件、自由文redact |
| 途中`aclose()` / cancellation | なし | なし | UNSET | なし |

#### Agent宣言とtyped input

- `Agent.response_schema`を`Mapping[str, Any] | None`へ改める(PR1契約の小改訂)。`None`は
  「構造化出力を要求しないtext役割」を表し、Geminiの`invoke_stream`は
  `response_mime_type` / `response_schema`を送らない。既存のstructured `invoke`とDeepSeek function-call
  runtimeは`response_schema=None`を未対応の誤用としてrenderer / span / provider requestより前に拒否する。
  既存4役割(schemaあり)の宣言と挙動は変えない。
- Direct Answer Agent: stable name `direct_answer`、`response_schema=None`、
  `output_type=DirectAnswerDraft`。Flowはcitation marker除去・blank検査後、hard-codeした型ではなく
  `agent.output_type`を使ってdraftを構築する。
- Evidence Answer Agent: stable name `evidence_answer`、`output_type=RawEvidenceAnswerDraft`、手書きschemaを
  維持する。duplicate-key検査後のfinal JSON parseは`agent.output_type`をvalidation targetとして使い、
  lenient raw draftから`EvidenceAnswerDraft`へのstrict finalizeはFlowが行う。schemaとraw draft契約の整合を
  contract testで守る。
- 各roleの`AgentPrompt`(手動version `"v1"` / 固定instructions / 同期renderer)を
  role別`prompts.py`に併置する。bump規則は既定(本文・template・schemaのmodel-visible変更)。
  旧`compute_call_signature()`と旧version値を持ち込まない。
- typed input:

```python
@dataclass(frozen=True, slots=True)
class DirectAnswerInput:
    request: AnsweringRequest
    previous_answer: str
    previous_error: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceAnswerInput:
    request: AnsweringRequest
    evidence: tuple[AnswerEvidenceItem, ...]
    target_time_window: str | None
    previous_error: str | None = None
```

- evidenceのmodel-visible投影(引用ref付きの根拠列挙)はrendererが`InputT`だけから決定的に
  行い、既存のsanitize / untrusted境界と文面規則を維持する。
- 固定instructionsは`system_instruction`、render済みtask inputは`contents`へ分離する。
  message構造の変更は意図的に受け入れ、byte同一性を要求しない(既定パターン)。
- `ModelTarget` / `ModelSettings`は現行値(temperature 0.2 / max_output_tokens 2048、
  現行model名)を維持する。

#### Flow(phase policy)に残るもの

- attempt loop(最大2回)、`previous_error`管理、retry条件(既存classifyの
  `RETRY_IN_REQUEST`判定)を変更しない。
- fragment間の`ensure_answer_generation_continues()`、`AnswerGenerationStopped`の伝播、
  `LiveAnswerDraftSession`のgeneration番号とreset / commit choreographyを変更しない。
  Direct / Evidenceのprovider attempt draftは`generation=attempt_number`。Evidenceのretriable failureでは
  次attempt前に`generation=attempt_number + 1`をresetし、最終failureのsafe fallbackも
  `generation=attempt_number + 1`を使うため、2 attempt失敗後のfallbackはgeneration 3となる。
  Direct retryは現行どおりresetを追加しない。
- evidence固有: `IncrementalJsonAnswerExtractor`によるdecoded deltaだけの配信、
  `parse_evidence_answer_final_json()`、`finalize_evidence_answer_draft()`はflowに残す。
- direct固有: fragment結合、citation marker除去、空文字の`DirectAnswerInvalidError`は
  flowに残す(runtimeは組み立て後の全文を見ない)。
- **direct=非retriable時raise / evidence=safe fallback draft**の非対称、fallback文言、
  fallback時のdelta振る舞いを維持する。
- role error語彙(`DirectAnswerInvalidError`等)とclassifyはflow所有のまま維持する。
- flowのconstructorはgenerator portの代わりに`agent`と`runtime_scope_factory`を受け取る。
  旧generator Protocolは削除する。
- 両outcome metricの既存属性(result / retry_used / failure_code / status / fallback_used)を変えず、
  `prompt_version` / `ai_model`を含む新labelを追加しない。metric consumerとcardinality policyを伴う拡張は
  observability整理で別に扱う。

#### Resource scopeと観測

- 両flowは共用factory(`activate_gemini_agent_runtime`)をphase 1回につき1 scope開き、
  attempt 1 / 2で同じruntime / clientを共有する。clientの誕生はgraph構築時からphase scope内へ
  移る(意図的変更として受け入れる)。
- scope activation / Runtime構築の失敗はprovider attempt開始前のresource failureであり、request内retryや
  Evidence safe fallbackの対象にしない。attempt span、delta、outcome metricを作らず、Direct / Evidenceとも
  同じ例外を伝播する。phase spanは未分類終了としてERROR / exception eventを残し、自由文はredactする。
- 各attemptでFlowが取得した`AgentTextStream`はruntime scopeの**内側**の`finally`でcloseする。
  cleanup順を、SDK iterator close試行 -> attempt span end -> classified error再送出または正常復帰 ->
  Flowのouter iterator close完了 -> Gemini client scope close -> phase span end、とする。別attemptを開く前にも
  前attemptのouter / SDK iterator closeが完了していなければならない。
- phase spanはflowが`agent_phase`を1本開く。allowlistは`phase="direct_answer" |
  "evidence_answer"`と`agent_name`の2つ。attempt spanはその子となる。
- `AnswerGenerationStopped`は同じexception instanceのままphase span終了後に再送出し、phase spanへ
  exception event / ERROR statusを残さない。stream開始前の停止はattempt span 0本、stream途中の停止は
  attemptをconsumer abandonmentとして閉じ、正常EOF後の停止は完了済みattemptを`result="succeeded"`のまま保つ。
- 本sliceでは既存attempt spanの`prompt_version`属性を維持し、新しいmetrics labelへ複写しない。
  親仕様OBS-05 / OBS-06の最終allowlistへの移行は、migration完了後のobservability整理で一括して行う。
- scopeの全経路close(成功・分類済み・未分類・停止・cancel・構築途中失敗)は共用factoryの
  既存契約に従い、この呼び出し箇所でも検証する。

### Non-goals

- `invoke` / `invoke_stream`の最終命名、呼び出し向きの反転(`agent.run_attempt(...)`)、
  ModelGateway改名(migration完了後の再訪議題)。
- 回答Run全体のGemini client共有(次の専用slice)。
- delta transport(SSE / live_updates)、`LiveAnswerDraftSession`、continuation機構の変更。
- `result_assembly`、citation検証、status導出の変更。
- retry回数、fallback文言、prompt文面の意味変更(system / user分離を除く)。
- DeepSeek系(query / selector)へのstreaming適用。
- rate limitの新規適用。API / DB / Redis / Taskiq / dependencyの変更。

### Done

- 両Answer Agentが何者かを各1つの`Agent`宣言から読め、旧generator / call specの正本が消える。
- non-streaming `AgentRuntime.invoke`を変えず、close可能なtext streamを返す
  `StreamingAgentRuntime.invoke_stream` capabilityと専用scope factoryが実consumerから使われる。
- streaming attemptが`agent_provider_call`として観測でき、分類済みerrorでexception eventが
  無く、放棄・cancellationでもspanがちょうど1回閉じる。
- 同じprovider fragment列を与えたときのdraft、retry、fallback、delta順序、
  `AnswerGenerationStopped`伝播、metricsの意味が移行前と同値である。system / user分離により
  live model出力のbyte同一性は要求しない。
- Gemini 4役割すべてが共通runtime / 共用factory経由になり、answer系clientがgraph構築時に
  生成されない。
- 既存answer / delta / continuation regressionとexit gateが通る。

## 責任境界

| 責任 | Agent宣言 | GeminiAgentRuntime | Flow(policy) | composition |
|---|:---:|:---:|:---:|:---:|
| 役割・Prompt・version・model・schemaの正本 | ○ | - | - | - |
| provider stream呼び出し・分類・SDK aclose | - | ○(`StreamingAgentRuntime`) | - | - |
| attempt span(手動lifecycle)・usage | - | ○ | - | - |
| fragment消費loop・continuation・delta | - | - | ○ | - |
| 最終parse / finalize / fallback / retry | - | - | ○ | - |
| phase span・metrics | - | - | ○ | - |
| factory配線・phases factory更新 | - | - | - | ○ |

配置: 宣言は`direct_answer/agent.py` / `evidence_answer/agent.py`、本文とversionは各
`prompts.py`。`ai/gemini.py`・`ai/spec.py`・旧prompt moduleは削除し、evidenceの手書きschemaは
schema正本moduleとして維持する。

削除inventory(定義とpackage re-exportの両方):

- 削除: `GeminiDirectAnswerGenerator`、`GeminiEvidenceAnswerDraftGenerator`、両call spec、
  `DirectAnswerGenerator` / `EvidenceAnswerDraftGenerator` protocol、`model_name` /
  `prompt_version` / `rate_limit_policy` property、休眠rate limit policy(値ごと削除)。
- consumer inventory: composition(phases factory)と`scripts/probe_question_answering.py`を
  新しいflow constructor / factory経由へ更新する。

## Test contract

- `AgentRuntime.invoke`が変わらないことと、`StreamingAgentRuntime.invoke_stream` / `AgentTextStream` /
  `StreamingAgentRuntimeScopeFactory`の契約をcontract testで固定する。DeepSeek Runtimeとnon-streaming fakeへ
  `invoke_stream`を要求しない。
- `invoke_stream`はfragment無加工、1 invocationにつき最大1 SDK streamとし、renderer / config失敗では
  戻り値iterator・span・SDK streamが0件。一度も反復せずcloseしたstreamもprovider request / spanが0件。
- provider機構の各経路(prompt block / blocked finish / 終端欠落truncation / 未分類translate)が
  既存分類で発生し、分類済みはspan終了後raiseでexception eventが無く、未分類は明示的
  exception event 1件・ERROR status・`result`なしで伝播する。translated errorのcause identityも維持する。
- streaming attempt spanの手動lifecycle: 正常消費・分類済み・未分類・途中`aclose()`・provider await中cancel・
  consumer処理中cancelの各経路で、fake spanの`end_calls == 1`を直接検証する。途中放棄 / cancellationは
  新しいresult / error.type / eventを作らず、実在usageだけを残す。
- 親context捕捉後に別contextでconsume / closeしてもattemptの親が生成時phaseであり、fragment受領直後の
  current spanがphaseのままであることを検証する。
- usageはchunkに存在するときblocked判定 / fragment yieldより前に記録し、blocked終端chunk、終端前放棄、
  usage受信後のclose中cancelを個別に固定する。欠損を0で補わない。
- 二層cleanupは経路別matrixで固定する。生成済みouter iteratorはFlowが1回closeし、取得済みSDK iteratorは
  Runtimeが1回close、未生成 / 未取得なら0回。SDK close失敗 / cancelでもspan endを1回行い、outer / SDK
  close完了がclient scope closeより前で、attempt 2開始よりattempt 1 closeが前であることを検証する。
- `response_schema=None`(direct)のrequestにmime / schemaが含まれず、evidenceのrequestは
  既存schemaを送る。`Agent`宣言が`response_schema=None`を受理する。
- evidenceの手書きschemaとlenient raw draft契約の整合test(field / 型 / 代表payload)。
- flow同値: 2 attempt・retry条件・`previous_error`内容、directの非retriable raise、
  evidenceのfallback(文言・delta reset / append / finish・metrics)が移行前と同値。
- delta: provider attempt draftのgeneration番号はattempt番号と一致し、Evidence retry reset / fallbackは
  `attempt_number + 1`、2回失敗後fallbackはgeneration 3とする。evidenceではextractorのdecoded deltaだけが
  配信される。fragment間のcontinuation停止が同じ`AnswerGenerationStopped`として伝播し、run / phase spanを
  error扱いせず、mid-stream attemptはabandonとして閉じる。
- fake clientによるrequest-level test(両agent): `system_instruction`に固定instructionsのみ、
  `contents`にrender済みinputのみを入れる。question / evidence / previous answer / previous errorのsentinelが
  `system_instruction`へ現れず、fixed instructionsのsentinelが`contents`へ現れないことを双方向に検証する。
  golden一読(初回 / retry)を実装PRの確認手順に含める。
- phase span allowlist `{phase, agent_name}`、attempt spanとの親子、trace全面(attribute /
  event / status description)に親仕様OBS-05のmodel-visible sentinelが無い。streaming attemptの暫定allowlistは
  `{agent_name, attempt_number, prompt_version, result}`とし、標準`gen_ai.*` / `error.type`とframework内部属性は
  親仕様どおり別扱いとする。
- metricsは既存属性を維持し、新labelを追加しない。
- client: graph構築時にanswer系client生成0回、phase scope内で誕生、attempt 1 / 2で
  同一identityとする。client取得済みscopeは全終了経路でclose 1回、client生成前のscope enter失敗は0回、
  client生成後のRuntime構築失敗は1回closeする。scope enter / Runtime構築失敗はattempt / delta /
  outcome metric 0件で同じ例外を伝播する。
- 旧symbolが定義・package re-exportの両方から残存せず、probeが新配線で動く。
- 既存answer / delta / continuation regressionが通る。

Exit gate: 親仕様`REG-01`〜`REG-03`、`RES-07`。

実装順序の推奨: (1)streaming capabilityと手動span / cleanup契約、(2)Direct Answer
(schema無しの単純形でstreaming契約を固定)、(3)Evidence Answer(structured streaming +
fallback)。各段で既存regressionをgreenに保つ。

残すseam: flow(phase policy owner)、`LiveAnswerDraftSession`ほかdelta / continuation機構、
`result_assembly`、evidence schema正本module。
削除するseam: 両generator adapter / call spec / generator protocol、休眠rate limit policy。
