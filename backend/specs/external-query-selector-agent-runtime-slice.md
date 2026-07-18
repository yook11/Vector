# External Query / Selector Agent と DeepSeek Runtime slice 仕様

更新日: 2026-07-19

実装状況: Implemented（PR2）

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md`のPR2を具体化する。

外部検索のquery生成とevidence選別を、それぞれ独立した宣言的`Agent`へ移し、両Agentを
`DeepSeekAgentRuntime`で実行する。既存`ExternalSearchResearchRunner`は一時的なworkflow ownerとして
query生成、検索、候補pool、selector retry、task reportを引き続き所有する。

本sliceではTavily検索をTool化せず、external branch単位のresource factoryも導入しない。これらは親仕様の
PR3 / PR4で扱う。

## Work Definition

### Problem

- `DeepSeekQueryGenerator`と`DeepSeekEvidenceSelector`が、Agentの役割宣言、Prompt、model設定、
  structured-output transport、client、provider呼び出し、parseをまとめて持つ。
- Query / Selectorのmodel、schema、Prompt versionが`ExternalSearchDeepSeekSpec`へ集約されている一方、
  共通`Agent`宣言から役割と入出力契約を読み取れない。
- DeepSeek API上のfunction callingはJSON outputを受け取るtransportだが、実行可能なExternal Search Toolと
  同じ`tool`語彙で表現されている。
- LLMが返すdraftのvalidationと、query normalization、selection cap、source metadata復元等のworkflow
  policyの境界がadapter内へ混在している。
- DeepSeek固有の5種類のresponse defectと、PR1で導入したprovider-neutralな3種類の
  `AgentResponseDefect`が併存している。

### Evidence

- `external_search/ai/deepseek.py`は、各roleのconstructorで別々の`AsyncOpenAI` clientを生成し、
  `_call_tool()`でforced function callingを実行する。
- `external_search/ai/spec.py`はmodel、max tokens、thinking設定、function名、response schema、base URL、
  timeout、旧call-signature version、休眠rate limit policyを1つのspecへまとめている。
- Query adapterはtool argumentsの`queries`がlistなら、非文字列要素だけを除外して`list[str]`を返す。
- Selector adapterはtool argumentsを`EvidenceSelectionResult.from_raw()`へ渡し、claim / why selected /
  missingの文字数・件数を決定的にclampする。
- `ExternalSearchResearchRunner`はQueryをworkflow上1回だけ呼び、strip、文字数cap、重複排除、件数capを
  適用してからSearchProviderへ渡す。
- 同RunnerはSelectorを最大2 attempt呼び、分類済み失敗または30秒のworkflow timeoutだけをretryする。
- DeepSeek client timeoutは20秒で、lock済みOpenAI SDKの既定transport retryを明示上書きしていない。
- Selectorへ渡すmodel-visible candidateはindex、title、source name、published at、snippetだけで、URLを
  含めない。source identityはSelector outputのindexから元candidate poolを引き直して復元する。
- Query / Selector経路には現在、provider usageを記録する共通attempt spanがない。

### Invariants

#### Agent宣言とtyped I/O

- External Query AgentとExternal Evidence Selector Agentを、共通`Agent[InputT, OutputT]`の独立した
  immutable / statelessなinstanceとして宣言する。
- Agent宣言はprovider client、retry state、candidate pool、event reporter、task reportを保持しない。
- Query Agentの入力を次の型付き概念として扱う。

```python
@dataclass(frozen=True, slots=True)
class ExternalQueryGenerationInput:
    task: ExternalResearchTask
    as_of: datetime
    target_time_window: str | None
```

- Query Agentのoutputは完成済みSearch Tool inputではなく、modelが返したdraftとする。

```python
class ExternalQueryDraft(BaseModel):
    queries: list[str]
```

- 現行互換として、raw `queries`がlistなら非文字列要素だけを除外し、文字列要素をdraftへ残す。
  この互換parseは`ExternalQueryDraft`の`mode="before"` field validatorが所有し、raw値がlistでない場合は
  通常のoutput validation errorとする。validatorはstrip、空文字除外、cap、重複排除を行わない。
  strip、空文字除外、文字数cap、同一文字列の重複排除、件数capはこの順序でworkflow ownerが適用する。
- Selector Agentへは、URLを型の段階から除いたcandidate projectionだけを渡す。

```python
@dataclass(frozen=True, slots=True)
class ExternalEvidenceCandidateInput:
    index: int
    title: str
    source_name: str | None
    published_at: datetime | None
    snippet: str | None


@dataclass(frozen=True, slots=True)
class ExternalEvidenceSelectionInput:
    task: ExternalResearchTask
    candidates: tuple[ExternalEvidenceCandidateInput, ...]
    as_of: datetime
```

- Selector Agentはindex参照のdraftだけを返し、URL、source ref、source metadataを生成しない。

```python
class EvidenceSelectionDraft(BaseModel):
    candidate_index: int = Field(ge=0)
    claim: str
    why_selected: str


class ExternalEvidenceSelectionDraft(BaseModel):
    selections: list[EvidenceSelectionDraft]
    missing: list[str]
```

- `DeepSeekAgentRuntime`はdraftを`OutputT`へvalidationするところまでを所有する。範囲外・重複indexのdrop、
  evidence / missing cap、自由記述欄のclamp、元candidate poolからのsource復元はworkflow ownerが
  `EvidenceSelectionResult`へfinalizeするときに適用する。
- Agent inputを暗黙にserializeしない。各`input_renderer`は上記型からroleが必要とするtask dataだけを
  model-visible textへ投影する。

#### Promptとversion

- Query / Selectorそれぞれに`AgentPrompt`を宣言し、固定instructionsと同期`input_renderer`を分離する。
- 固定instructionsは役割、判断基準、禁止事項、出力ルールをすべて持つ。`input_renderer`は実行時の
  `as_of`、collection goal、time window、candidate projectionだけを決定的にrenderし、I/Oを行わない。
- collection goal、time window、title、source name、snippetは既存の
  `sanitize_for_untrusted_block()`と`<untrusted_input>`境界を維持する。
- Selectorのcandidate projectionはsanitize後の各fieldをJSONとして決定的にrenderし、title / snippet内の
  改行や`[index]`風文字列によるcandidate recordの偽装を許さない。
- DeepSeek requestでは`agent.prompt.instructions`を`system` message、render済みtask inputを`user`
  messageへ渡す。現行の単一user messageからmessage構造が変わることを意図的に受け入れ、byte同一性を
  要求しない。
- Prompt versionは各roleの固定instructions / input templateと同じ`prompts.py`に手動revisionとして置き、
  Agent組み立て側はその定数を参照する。初期値は各roleの`"v1"`とする。
- 固定instructions、固定input template、response schemaのmodel-visibleな構造、enum、required、
  descriptionを変更するときはversionも更新する。実行時input、output typeだけの変更、model、max tokens、
  timeout、retry回数では更新しない。
- Query / Selector Agentへ旧`compute_call_signature()`と既存hash値を移行せず、旧versionとの連続性を
  要求しない。

#### DeepSeek structured-output transport

- `DeepSeekAgentRuntime`をprovider-backedな1 attempt Runtimeとして導入し、`AgentRuntime` contractを
  実装する。
- DeepSeek function callingは実行可能なToolではなく、declared outputを受け取るprovider固有transportと
  定義する。Query / Selector Agentへ`tools` fieldを追加せず、External Search Tool registryへ混入させない。
- JSON schemaの正本は`agent.response_schema`、Python parse契約の正本は`agent.output_type`とする。
- DeepSeek固有のfunction名と中立的descriptionはprovider bindingとしてRuntimeへ注入し、schemaや業務
  ルールをbindingへ複製しない。

```python
@dataclass(frozen=True, slots=True)
class DeepSeekOutputBinding:
    function_name: str
    description: str
```

- Queryは既存`generate_search_queries`、Selectorは既存`select_evidence`をoutput function名として維持する。
- function descriptionへroleの判断規則を置かず、固定の業務ルールはAgent instructionsへ移す。
- Runtimeは`agent.response_schema`をfunction parametersへwrapし、strict function、forced
  `tool_choice`、`thinking=disabled`をDeepSeek requestへ適用する。
- Runtimeは期待したfunction callのargumentsを取り出し、JSON objectとしてdecodeしてから
  `agent.output_type`へvalidationする。functionをPython上で実行しない。
- Queryのmax output tokensは256、Selectorは2048、modelは`deepseek-v4-flash`、base URLは既存値を
  維持する。

#### Error、retry、timeout

- `DeepSeekAgentRuntime.invoke()`は1回のAgent attemptだけを実行し、workflow retry、fallback、別Agent / Tool
  起動を行わない。
- Query / Selectorのattempt loopとretry条件は、現行どおり`ExternalSearchResearchRunner`が所有する。
  PR2はretry loopをadapterから移す変更ではなく、同Runnerが呼ぶ対象を旧portからAgent / Runtimeへ置き換える。
- Query Agentはworkflow上1 attemptだけ実行する。分類済みinvalid response、既知provider error、
  workflow timeoutではtaskを`query_generation_failed`とし、そのtaskのSearch Tool / Selector Agentを
  起動しない。
- Selector Agentは同じRuntime / clientで最大2 attempt実行する。分類済みinvalid response、既知provider
  error、workflow timeoutだけをretryし、2回失敗した場合に`selector_failed`とする。
- 未分類例外はQuery / Selectorの通常失敗へ変換せず、同じ例外を上位へ伝播する。
- DeepSeek client timeout 20秒と、Query / Selector phaseの`asyncio.wait_for()` 30秒を別責任として維持する。
  前者はprovider transport、後者はworkflow backstopであり、1つの定数へ統合しない。
- lock済みOpenAI SDKの既定transport retryをPR2で上書きしない。1 Agent attemptは1 SDK invocationを表し、
  SDK内部の物理HTTP retryごとに`attempt_number`を増やさない。
- function argumentsをJSON decodeできない場合は`response_not_json`、objectでない場合は
  `response_not_object`、declared outputへvalidationできない場合は`output_schema_mismatch`とする。
- expected function callがない場合と別function名の場合も、declared outputを取得できない
  `output_schema_mismatch`として分類する。provider名、role名、function名をdefect codeへ埋め込まない。
- Selectorの最終`selector_failure_reason`は、invalid responseでは共通defect値、既知provider errorでは
  安全なprovider reason、30秒backstopでは`selector_timeout`を使う。Query reportには新たなreason fieldを
  追加しない。
- draftの文字列が`EvidenceSelectionResult`の完成型制約（例: 空文字禁止）を満たさない場合も、旧Selector
  adapterと同じmodel response failureとして`output_schema_mismatch`に分類し、Selector retry対象とする。
- 分類済み例外はdefect / allowlist済みrepair hintだけを保持し、Prompt、collection goal、query、candidate、
  URL、生のfunction arguments、SDK自由文message、API keyを保持しない。変換元例外も
  `__context__` / `__cause__`へ残さない。
- Selectorには現行`previous_error`経路が存在しない。retryへ`previous_error`、repair hint、前回model outputを
  新設せず、attempt 1 / 2で同じtyped input instanceを再利用する。

#### PR2のresource lifecycle

- Query用とSelector用に別々の`AsyncOpenAI` client / `DeepSeekAgentRuntime` instanceを維持する。1 attemptごと、
  1 taskごとにclientを作り直さず、Selectorのattempt 1 / 2と並行taskは同じSelector Runtime / clientを使う。
- client / Runtimeの構築位置は`_DeferredQuestionAnsweringAgent.answer()`内の既存回答graph構築より前へ動かさず、
  API、worker、`build_question_answering_starting_agent()`では生成しない。
- 現行回答graphはplanning結果が確定する前にexternal componentを構築するため、PR2ではdirect / internal pathでも
  Query / Selector client objectが生成される既存挙動を意図的に受け入れる。物理HTTP接続の確立までは
  この契約に含めない。
- PR2ではDeepSeek clientのclose ownerを新設しない。clientを誰もcloseしない既存のresource debtを明記して
  受け入れ、lazy activation、Query / Selector client共有、Tavilyとのbranch scope、全経路closeはPR4の
  `ExternalResearchRuntimeFactory`でまとめて解消する。
- `DeepSeekAgentRuntime`は借りたclientをcloseしない。PR2にphase scope factoryや不完全なbranch factoryを
  追加しない。

#### Observability

- Runtime invocationごとに`agent_provider_call`を1本作り、正の`attempt_number`をworkflow ownerから受け取る。
- SDK内部の物理HTTP retryは同じ`agent_provider_call`内のtransport挙動とし、Agent attempt spanを増やさない。
- provider responseにusageがあれば、arguments parse / output validationより前にattempt spanへ記録する。
  usageをRuntime戻り値、draft、task reportへ同乗させず、phase / run spanへ複写しない。
- attempt spanの独自attribute、GenAI標準attribute、分類済みresult / error契約はPR1の
  `GeminiAgentRuntime`と同じprovider-neutral規則を使う。
- `ExternalSearchResearchRunner`はtaskごとにQuery / Selectorの`agent_phase`を1本ずつ作る。Query phaseは
  attempt 1を、Selector phaseはretryを含むattempt 1 / 2を同じphaseの子として包み、attemptごとにphaseを
  作り直さない。
- external Query / Selectorの`agent_phase`だけは、共通allowlistの`phase` / `agent_name`に加えて、
  non-negativeな`task_index`を持つ。`task_index`を`agent_provider_call`へ複写しない。
- 後続sliceでexternal workflow policyを`AnsweringRunner`へ移すときは、span名、phase名、attribute、
  attemptとの親子関係を含むこのphase span契約もpolicyと一緒に移す。
- span attribute、event、status descriptionへPrompt、query、candidate text、URL、selection本文、raw response、
  Prompt versionを記録しない。

### Non-goals

- Tavily `SearchProvider`をExternal Search Toolへ移すこと（PR3）。
- Query / Selector / Tavily clientをexternal branch scopeで生成・共有・closeする
  `ExternalResearchRuntimeFactory`を導入すること（PR4）。
- direct / internal pathでQuery / Selector client objectを0個にすること（PR4）。
- external pipelineを`AnsweringRunner`へ展開し、`ExternalSearchResearchRunner`を削除すること。
- task / query concurrency、query normalization、candidate pool、partial failure、event順序を変更すること。
- Selectorのretry回数、SDK retry、provider / workflow timeoutを変更すること。
- DeepSeek model、base URL、max tokens、thinking設定を変更すること。
- Prompt versionを自動hashへ戻すこと、旧version値を維持すること。
- Query / Selectorへhandoff、guardrail、session、generic tool loopを追加すること。
- API response、DB、Redis、Taskiq message、frontend type、dependencyを変更すること。

### Done

- Query / Selectorが何者か、何を入力し何をdraftとして返すかを、それぞれ1つの`Agent`宣言から読み取れる。
- `DeepSeekAgentRuntime`が両Agentを1 attemptだけ実行し、structured-output transportをAgentの実行可能Toolと
  混同せず、検証済みdraftを返す。
- Query normalizationとSelector finalizationがRuntimeから分離され、既存のcap、drop、source復元を維持する。
- 固定instructionsとtask inputがsystem / user messageへ分離され、各Prompt versionが固定本文の隣にある。
- Query失敗時の短絡、Selectorの最大2 attempt、timeout、failure reason、未分類例外伝播を維持する。
- Query / Selectorの各Agent attemptを、model-visible textを漏らさず同じ回答trace配下で観測できる。
- 旧call specのmodel / schema / Prompt設定に複数の正本が残らない。
- 既存external searchのquery、selection、task report、partial failure regressionが通る。

## 責任境界

| 責任 | Agent宣言 | DeepSeekAgentRuntime | ExternalSearchResearchRunner |
|---|:---:|:---:|:---:|
| role / instructions / output schema | ○ | - | - |
| typed inputのtask text化 | 宣言 | 実行 | 入力構築 |
| DeepSeek function transport | - | ○ | - |
| 1 SDK invocation | - | ○ | - |
| output draft validation | 型を宣言 | ○ | - |
| SDK内部transport retry | - | client既定 | - |
| workflow timeout / Agent retry | - | - | ○ |
| task単位の`agent_phase` | - | attempt spanだけ | ○ |
| query normalization | - | - | ○ |
| selection clamp / index検証 / source復元 | - | - | ○ |
| SearchProvider呼び出し / partial failure | - | - | ○ |
| usage記録 | - | attempt span | - |

## Test contract

### Agent / Prompt

- Query / Selector Agentのstable name、provider、model、max tokens、Prompt version、output type、schemaを固定する。
- Agent declaration、Prompt、schemaをmutationできない。
- system messageに固定instructionsだけ、user messageに実行時task dataだけが入る。
- Queryのcollection goal / time window、Selectorのgoal / candidate textをsanitizeする。
- Selector user messageにURLとURL sentinelが存在せず、index / title / source name / published at / snippetだけを
  含む。
- candidate textが改行と偽`[index]`を含んでも、JSON escapeによりrecord境界を偽装できない。
- 固定instructions / input template / response schema変更時のmanual version更新規則を仕様確認する。

### Runtime / structured output

- Agentのschemaがstrict function parametersへ渡され、既存function名をforced `tool_choice`へ使う。
- bindingがschemaと業務ルールを複製せず、Agentの`tools` fieldやExternal Search Tool registryへ入らない。
- `thinking=disabled`、model、max tokens、base URL、client timeoutを維持する。
- valid argumentsからQuery / Selector draftを返す。
- `ExternalQueryDraft`のbefore validatorが、list内のnon-string Query要素だけを除外する既存挙動を維持する。
- invalid JSON、non-object、schema mismatch、no function call、wrong function nameを合意済み3 defectへ写像する。
- 負数`candidate_index`をoutput schema mismatchとし、workflowの範囲外index dropへ到達させない。
- 既知SDK errorを安全なprovider errorへ変換し、未分類例外を同じinstanceで伝播する。
- 分類済み例外のmessage / context / causeにPrompt、candidate、raw arguments、SDK message、secret sentinelがない。

### Workflow regression

- Query Agentは1 attemptで、分類済みfailure / 30秒timeout後にSearchProviderとSelectorを0回にする。
- Query Agentはinvalid response、既知provider error、workflow timeoutの各経路でRuntimeを1回だけ呼び、
  未分類例外も1回目からそのまま伝播する。
- Query draftへstrip、文字数cap、重複排除、件数capを既存順序で適用する。
- Selector Agentは分類済みfailure / timeout時だけ最大2 attemptで、未分類例外ではretryしない。
- Selector Agentはinvalid response、既知provider error、workflow timeoutの各経路で最大2 attemptとし、
  attempt 1 / 2へ同じtyped input instanceを渡す。`previous_error` / repair inputを追加しない。
- Selector draftから既存のclamp、範囲外・重複・cap超過index drop、source metadata復元を行う。
- draft内の空claim / why selectedが完成型制約に違反する場合は`output_schema_mismatch`としてretryする。
- candidate 0件時はSelectorを0回とし、既存の成功・evidence 0 semanticsを維持する。
- task並列数、event順序、task report順序、partial provider failureを変更しない。

### Trace / usage

- Query成功時はattempt 1、Selector retry時はattempt 1 / 2の`agent_provider_call`を記録する。
- taskごとに`task_index`付きQuery / Selector phaseを1本ずつ記録し、Selector retryでもphaseは1本のままとする。
- `task_index`はphaseだけに置き、attempt spanへ複写しない。phase policyの移動時にspan契約も一緒に移す。
- response usageをparse / validation失敗時にもattempt spanへ記録し、他の戻り値・spanへ複写しない。
- classified errorでは安全なresult / `error.type`だけを記録し、unclassified errorの自由文は既存export
  redactionを維持する。
- attribute、event、status descriptionを含むspan全面にmodel-visible sentinelが存在しない。

## 確定した実装境界

`ExternalSearchResearchRunner`は旧QueryGenerator / EvidenceSelector portや互換adapterを経由せず、注入された
Query / Selector Agentと`DeepSeekAgentRuntime`を直接呼ぶ。Runnerはclientを構築せず、compositionが既存の
deferred回答graph構築内でQuery用・Selector用client、binding、Runtimeを組み立てて注入する。

```text
backend/app/agent/runtime/
├─ contract.py                         # provider-neutral AgentRuntime
├─ gemini.py                           # 既存Gemini Runtime
└─ deepseek.py                         # DeepSeekAgentRuntime / DeepSeekOutputBinding

backend/app/agent/evidence_collection/external_search/
├─ agent.py                            # Query / Selector Agent宣言とresponse schema
├─ prompts.py                          # 固定本文 / renderer / 手動Prompt version
├─ deepseek_binding.py                 # role別DeepSeek output binding定数
├─ contract.py                         # typed input / draft / workflow result
└─ runner.py                           # phase / retry / timeout / normalization / finalization
```

`DeepSeekOutputBinding`はprovider transport契約なのでAgent fieldへ入れない。role別function名と中立的descriptionは
`deepseek_binding.py`へ置き、compositionが対応するAgent / binding / clientをRuntimeへ配線する。Runtimeは
Agent名からbindingを暗黙に検索せず、bindingへschema、Prompt、workflow ruleを複製しない。

PR2で削除する。

- `DeepSeekQueryGenerator` / `DeepSeekEvidenceSelector`。
- `QueryGenerator` / `EvidenceSelector` port。
- `ExternalSearchDeepSeekSpec`と、そこへ集約された旧call-signature version、model、schema、function設定の
  重複した正本。
- adapter内のPrompt構築、function arguments parse、output validation / finalization責任。

PR2で残す。

- 一時workflow ownerの`ExternalSearchResearchRunner`。
- `SearchProvider` / `TavilySearchProvider` / `ExternalSearchService`。
- task / query concurrency、query normalization、candidate pool、selection finalization、task report、events。
- deferred回答graph構築と、close owner不在を含む現行external resource timing。
