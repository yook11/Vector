# Planner Agent と AgentRuntime slice 仕様

更新日: 2026-07-18

実装状況: Implemented（PR1）

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md` のPR1を具体化する。

Plannerを最初の利用者として、宣言的な`Agent`クラスと、1回のprovider attemptだけを実行する
`AgentRuntime`の責任境界を導入する。

このsliceでは回答workflowを`AnsweringRunner`へ移さない。既存の
`QuestionAnsweringOrchestrator -> QuestionPlanningService`という呼び出し順を維持したまま、
Planner内部の宣言と実行だけを整理する。

## Work Definition

### Problem

- Question Plannerのname、prompt、model設定、response schema、output typeが複数ファイルへ分散している。
- `GeminiQuestionPlanner`がPlannerの役割宣言、prompt生成、client、API呼び出し、parseをまとめて持つ。
- 1回のLLM attemptと、最大2回のretry / fallback policyの境界が型と名前から読み取りにくい。
- 今後Query / Selector / Answer Agentを追加すると、役割ごとに同じ実行構造が複製される。

### Evidence

- `planning/ai/gemini_spec.py`がmodel、generation config、response schema、prompt version、
  rate limit policyを1つのcall specとして持つ。
- `planning/ai/gemini_prompt.py`が`PlanningRequest`と`previous_error`からpromptを作る。
- 現行promptは固定instructionsの途中に実行時inputが入り、その後にも固定ルールが続くため、
  `instructions`とrender済みinputを単純連結するだけでは同じprovider-visible promptにならない。
- `planning/ai/gemini.py`がGemini client、API呼び出し、response envelope検証、draft parseを持つ。
- 現行`GeminiQuestionPlanner`は回答graphごとに1 instance作られ、最大2 attemptで同じclientを使うが、
  明示的なclose ownerを持たない。
- 現行の回答graphはcontext preparationとhookの後に遅延構築されるため、PR1のPlanner clientもその順序を
  変えず、planning phaseより前には生成しない。
- `planning/service.py`がattempt loop、retry判定、fallback、finalization、audit、metricsを持つ。
- `QuestionPlanDraft`と`plan_from_draft()`は、LLM draftと完成済みdomain planを分けている。

### Invariants

- `Agent`は1つのLLM役割を表すimmutable / statelessな宣言とする。
- Planner Agentはstable name、`AgentPrompt`、model設定、`QuestionPlanDraft` output contractの正本を持つ。
- `AgentPrompt[InputT]`は`version`、宣言時に確定する`instructions`、同期`input_renderer`を関連づける
  immutableなPrompt宣言とする。
- instructionsはAgentの役割・判断基準・出力ルールをすべて保持し、実行時contextへ依存するCallableや
  async生成は許可しない。
- `input_renderer: Callable[[InputT], str]`は型付きinputだけから今回のmodel-visibleなtask inputを
  同期的かつ決定的に生成し、固定instructionsを複製せず、DB、外部API、provider clientへアクセスしない。
- Prompt versionの正本は固定instructions / input templateと同じ`planning/prompts.py`に
  `PLANNER_PROMPT_VERSION: Final[str]`として宣言し、`planning/agent.py`はその値を参照して
  `AgentPrompt`を組み立てる。Agent宣言側へversion literalを再定義しない。
- 本slice（PR1）では、Prompt versionは宣言者が明示する手動revisionとし、自動hashや旧call signatureから
  導出しない。固定instructions、固定input template、`response_schema`のmodel-visibleな構造、enum、
  required、descriptionを変更するときはversionも明示的に更新する。`output_type`だけの変更、model、
  model settings、実行時inputの変更ではversionを更新しない。
- `Agent`はprovider client、user input、retry回数、audit recorder、生成途中の状態を保持しない。
- `response_schema`はAgent宣言後に書き換えられないimmutable mappingとして保持する。
- `AgentRuntime.invoke()`は型付きinputを受け、providerを1回だけ呼び、検証済み`OutputT`だけを返す。
  usage等を含むresult envelopeは導入しない。
- `AgentRuntime.invoke()`の呼び出し側は、workflow policyが決めた正の`attempt_number`をkeywordで渡す。
  Runtimeはretry回数を保持・推測せず、その値をattempt spanの観測にだけ使う。
- provider attemptのusage / latencyは`AgentRuntime`がattempt spanへ記録し、戻り値、Serviceのdomain output、
  `RunResult`へ含めない。
- `AgentRuntime`はretry、fallback、completed planへの変換、別Agent / Toolの起動を行わない。
- `QuestionPlanningService`は最大2 attempt、retry判定、fallback、audit、metrics、
  `plan_from_draft()`を引き続き所有する。
- Planner用Gemini async clientはcompositionが実装するasync context-manager factoryから取得し、
  `QuestionPlanningService.plan()`の1 phaseにつきscopeを1回だけ開始する。同じscopeのRuntime / clientを
  attempt 1と2で共有し、別のplanning phaseへ再利用しない。
- compositionはPlanner clientの生成と破棄、Serviceはplanning phaseに合わせたscopeの開始と終了、
  Runtimeは借りたclientによる1 attemptだけを所有する。Runtimeは`invoke()`内でclientをcloseしない。
- composition moduleのimportとPlanner Runtime scope factoryの生成だけでは、`google.genai`とPlanner用
  Gemini具象Runtimeをloadしない。Plannerのprovider依存はRuntime scopeを実際に開始した時点で遅延loadする。
  `AnsweringRunner`や回答graphの構築時に既存の他Gemini工程がSDKをloadすることは本契約の対象外とする。
- Planner clientを取得できたscopeは、正常終了、分類済みfailure、想定外例外、task cancellation、
  client取得後のRuntime構築失敗の各経路でasync context managerを退出し、async clientのcloseを1回試行する。
- PR1ではclose自体が失敗した場合に独自の例外合成や抑止を行わず、close例外が本体例外を置き換え得て、
  元の例外が`__context__`に残るPython / SDK既定挙動を意識的に受け入れる。
- PR1ではSDK既定のconnection pool設定を使い、接続数上限やkeep-alive tuningを追加しない。
- Agent outputは`QuestionPlanDraft`であり、completed `QuestionPlan`をmodelへ直接作らせない。
- `QuestionContext`、`as_of`、`previous_error`のsanitizeとuntrusted input境界は
  Planner Agentの`prompt.input_renderer`を正本とする。
- structured-output schemaを実行可能Toolとして扱わない。
- provider error、blocked output、invalid JSON、invalid schemaのretry / fallback上の意味を維持する。
  旧例外名、旧audit code、旧prompt version値との連続性は要求せず、新しい共通契約へ置き換えてよい。
- invalid JSON、JSON object以外、output type validation失敗は、provider名やPlanner名を含まない
  `AgentResponseInvalidError`へ統一する。例外はlow-cardinalityな`AgentResponseDefect`と、安全に構成した
  `repair_hint`だけを保持し、生のprovider response、Pydantic input、自由文message、任意のvalidation
  contextを保持しない。変換元の`JSONDecodeError` / `ValidationError`も`__context__` / `__cause__`へ残さず、
  分類済み例外のexception graph全体を同じ安全な契約に含める。
- 既存の役割・判断・出力・repair semanticsとtask data、model、temperature、token上限、retry回数を維持する。
  clean splitのため、repairを`previous_error`有無で適用する条件文の追加・再配置は明示的に許可する。
- 現行promptの`固定文 -> task input -> 固定文`という配置は維持せず、固定文をinstructions、task inputを
  inputへ分ける。これはprovider-visibleなmessage構造の意図的変更であり、byte同一性をDoneに含めない。
- 現在休眠しているAgent経路のrate limit policyを、このsliceで新たに適用しない。
- attempt spanへquestion、prompt、requirements、previous error、draft本文を記録しない。
- 回答workerが未分類例外またはPlanner clientのclose失敗を最終捕捉するときは、固定event、`run_id`、
  low-cardinalityな`error_type`だけをerror levelで記録し、`exc_info`、例外message、stacktraceをstdout /
  LogRecordへ渡さない。span export用の例外redactionをstdout保護として扱わない。
- Planner phaseは`agent_phase`、実provider requestごとのgeneration attemptは`agent_provider_call`として
  観測する。generation attemptはremote API callを表す`CLIENT` spanとし、API操作、provider、model、usageは
  標準`gen_ai.*` attributeへ記録する。

### Non-goals

- planning / retrieval / answerのworkflow責任を`AnsweringRunner`へ移すこと。
- `QuestionAnsweringOrchestrator`、`QuestionAnsweringAgent`、`starting_agent`を削除すること。
- Query、Selector、Question Context、Direct Answer、Evidence Answerを同時に移行すること。
- Planner用Gemini clientをQuestion Context、internal embedding、Direct Answer、Evidence Answerと共有する
  回答Run全体のGemini resource scopeを導入すること。
- workerで`AnsweringRunner.run()`全体をprovider client scopeに入れ、Runner構築と回答graph構築を
  1つのfactoryへ統合すること。
- Gemini clientをprocess間または別の回答Run間で共有すること、connection pool上限を独自設定すること。
- clientのclose自体が失敗した場合のretry、元の例外との優先順位・合成・抑止、二重cancelやprocess強制終了に
  対するclose完了保証を追加すること。
- stdout / LogRecord全体へ共通の例外redaction processorを導入すること。
- `Agent` Protocolやroleごとの具象Agent subclassを先行導入すること。
- dynamic / async instructions、async input rendererを先行導入すること。
- prompt生成のためにrendererからDBや外部APIへアクセスすること。
- streaming runtime、Tool、handoff、guardrail、session、generic agent loopを追加すること。
- 固定instructionsやtask inputの文面自体を、この責任分離と無関係に書き換えること。
- Prompt versionを自動hashや旧`compute_call_signature()`から導出すること。
- 旧prompt version値、旧audit code、既存audit recordとの連続性を維持すること。
- Agent経路へprovider rate limit gateを新規適用すること。
- providerをGemini以外へ変更すること。
- API、DB、Redis event、Taskiq message、dependencyを変更すること。

### Done

- Planner Agentが何者かを、1つの`Agent`宣言から読み取れる。
- Agent、Runtime port、Gemini実装、Planner宣言が確定したmodule境界へ配置され、`running/`や
  `planning/`へ共有provider Runtimeが混在しない。
- Planner AgentのPrompt version、固定instructions、実行時inputの変換規則を同じ`AgentPrompt`宣言から
  読み取れる。
- 固定Prompt本文と`PLANNER_PROMPT_VERSION`が`planning/prompts.py`の同じ編集箇所にあり、
  `planning/agent.py`はそれらを参照するだけでversion literalを持たない。
- Gemini requestでは固定instructionsを`system_instruction`、render済みtask inputを`contents`として
  物理的にも分離して渡せる。
- `QuestionPlanningService`から、1 attemptの実行を`AgentRuntime.invoke()`として呼べる。
- `AgentRuntime.invoke()`は`QuestionPlanDraft`を直接返し、usage用envelopeを呼び出し側へ要求しない。
- runtimeを2回呼ぶかfallbackするかは、引き続き`QuestionPlanningService`から読み取れる。
- planning phaseの開始時にGemini client scopeを1回だけactivateし、最大2 attemptで同じasync clientを
  再利用した後、正常・例外・cancelの各終了経路でcloseを1回試行できる。
- context preparationまたはhookで短絡した場合はPlanner clientを生成せず、Runtime単体の`invoke()`は
  借りたclientをcloseしない。
- completed planのschema / finalization、retry、fallback、metricsの意味を維持する。
  provider-visibleなmessage構造を変えるため、live model outputのbyte同一性、旧audit code、
  旧prompt version値との連続性は要求しない。
- provider callごとのattemptを、同じ回答Run trace配下で安全に観測できる。
- first attempt成功時は1本の`agent_phase`配下に1本の`agent_provider_call`、retry成功または
  retry後fallback時は同じphase配下に2本の`agent_provider_call`が記録される。
- responseにusageがあれば、blocked / parse / validation判定より前にattempt spanへ記録できる。
- Planner固有のmodel / prompt / output schema設定に複数の正本が残らない。
- 旧`GeminiQuestionPlannerSpec`のうちmodel callを構成・実行する責任がAgent、Gemini runtime、
  compositionへ分かれ、旧prompt version / call signature機構を新しいAgent契約へ持ち込んでいない。
- audit記録側が`agent.prompt.version`を直接参照でき、旧Planner adapterの属性探索を必要としない。

## 責任境界

### 配置と名前

宣言と実行を同じmoduleへ戻さず、次の配置を本sliceで固定する。

| 配置 | 所有するもの |
|---|---|
| `backend/app/agent/agent.py` | `AgentPrompt`、`Agent`、`ModelTarget`、`ModelSettings` |
| `backend/app/agent/runtime/contract.py` | provider-neutralな`AgentRuntime` Protocolとscope factory contract |
| `backend/app/agent/runtime/gemini.py` | provider-backedな`GeminiAgentRuntime` |
| `backend/app/agent/planning/contract.py` | Planner Agentへ渡す`PlanningAttemptInput` |
| `backend/app/agent/planning/prompts.py` | 固定instructions、input template、`PLANNER_PROMPT_VERSION`、input renderer |
| `backend/app/agent/planning/agent.py` | `AgentPrompt`とstable nameが`question_planner`のPlanner Agent宣言 |
| `backend/app/agent/composition.py` | Gemini async client scope、Runtime、Planner Agent、Serviceの配線 |

`running/`はユーザー入力から最終回答までの`AnsweringRunner`境界を表すため、provider attemptの
Runtimeを置かない。Gemini runtimeを`planning/`配下へ置くと、後続のQuery / Selector Agentが
planning packageへ依存するため、共有provider実装は`agent/runtime/`へ置く。
`agent/runtime/__init__.py`はprovider-neutralなcontractだけを再公開し、Gemini具象をimportしない。
compositionは`app.agent.runtime.gemini`から`GeminiAgentRuntime`を明示的にimportする。

`GeminiAgentRuntime`のinstanceが保持する実行依存は借りたGemini async clientだけとする。Planner Agent、
instructions、model、settings、schema、task input、retry state、usage accumulator、Runner / Service参照を
constructorで保持しない。これらは`invoke()`ごとに渡されたAgent宣言とinputから取得し、同じRuntimeを
複数のGemini Agentで再利用できるようにする。

### `Agent`

`Agent[InputT, OutputT]`は`@dataclass(frozen=True, slots=True)`で定義する。
Planner、Query、Selector等はrole固有のsubclassではなく、共通`Agent`クラスのinstanceとして宣言する。

```python
from collections.abc import Callable, Mapping
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelTarget:
    provider: str
    name: str


@dataclass(frozen=True, slots=True)
class ModelSettings:
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AgentPrompt[InputT]:
    version: str
    instructions: str
    input_renderer: Callable[[InputT], str]


@dataclass(frozen=True, slots=True)
class Agent[InputT, OutputT]:
    name: str
    prompt: AgentPrompt[InputT]
    model: ModelTarget
    model_settings: ModelSettings
    output_type: type[OutputT]
    response_schema: Mapping[str, Any]
```

Protocolと具象Agentクラスへの分離は行わない。共通dataclassでは表現できない2種類目のAgent表現が
実際に必要になった場合だけ、既存consumerを確認してProtocolを追加する。

Plannerを含む1つのLLM役割の宣言として、少なくとも次の概念を関連づける。

- stable name
- version、宣言時に確定する固定instructions、型付きinputをmodel-visibleなtask inputへ変換する
  同期input rendererをまとめる`AgentPrompt`
- provider / modelを識別する`ModelTarget`
- provider-neutralな生成調整値を持つ`ModelSettings`
- Python側のparse契約であるoutput type
- model向けwire契約であるresponse schema

`InputT`は実行ごとに変わるtask inputを表すgeneric type parameterであり、`input_type`というinstance fieldは
持たない。`OutputT`はruntimeが返す検証済みoutputを表すgeneric type parameterで、runtime parseに必要な
具象型を`output_type` fieldへ保持する。`output_type`はPydantic `BaseModel` subclassだけに限定せず、
`TypeAdapter`でvalidationできるdataclass等のPython型を許可し、Runtimeは特定型の`model_validate()`を
無条件に呼ばない。

本slice（PR1）の`AgentPrompt.version`は固定Prompt宣言の手動revisionである。固定instructions、固定input
template、`response_schema`のmodel-visibleな構造、enum、required、descriptionを変えるときは、同じ
`prompts.py`でversionも明示的に更新する。question、`as_of`、`previous_error`等の実行時値、
`output_type`だけの変更、model、model settingsの変更ではversionを変えない。

instructionsはAgentの役割・判断・出力規則の固定部分をすべて表し、`InputT`の値によって変化しない。

`output_type`と`response_schema`は一方から他方を生成する同一契約ではない。
`output_type`はprovider responseをPython側で最終検証する正本、`response_schema`はmodelへ期待する
wire outputと行動上のdescriptionを伝える正本とする。既存の手書きGemini schemaを維持し、
`QuestionPlanDraft.model_json_schema()`から自動生成しない。field集合、Python必須fieldとwire required、
enum、nullable / type、代表payloadのvalidationをcontract testで照合する。
`frozen=True`は入れ子のmappingまでfreezeしないため、`response_schema`自体もimmutableな値を渡す。

`AgentPrompt.input_renderer`を関数にする理由は、model-visibleなtask inputの決定を実行時まで遅延しつつ、
依存先を`InputT`だけに限定するためである。同じ`InputT`には同じ文字列を返し、sanitize以外の
副作用やI/Oを行わない。外部状態が必要な場合は`QuestionPlanningService`またはcomposition rootで
先に解決し、`InputT`へ明示的に含める。

Plannerではattempt単位の入力を次の概念として扱う。

```python
@dataclass(frozen=True, slots=True)
class PlanningAttemptInput:
    request: PlanningRequest
    previous_error: str | None = None
```

`previous_error`はAgentの役割を変えるinstructionsではなく、次attemptで修正対象を示すtask inputである。
現行repair promptの「previous_errorがある場合は同じquestionについてschemaに合うJSONだけを返す」という
固定修正ルールは、条件付きルールとしてinstructionsへ移す。初回inputにはprevious error sectionを含めず、
retry inputにはsanitize済み`previous_error`値だけをmodel-visibleなtask inputとして追加する。
OpenAI Agents SDKの`MaybeAwaitable[str]`相当は導入しない。役割文が実行ごとに変わる具体的要求が
生じた場合だけ、同期Callableへのunion拡大を検討する。async化より先に、外部状態をcomposition rootで
解決して`str`を宣言へ渡せないか確認する。

instructionsとrender済みinputは責任上だけでなくprovider request上でも分ける。Gemini-backed runtimeは
`agent.prompt.instructions`を`system_instruction`、`agent.prompt.input_renderer(input)`の結果を
`contents`へ渡す。
既存の固定ルールはすべてinstructionsへ移し、input rendererは`PlanningAttemptInput`から作るtask inputだけを
返す。現行promptの文面順序とmessage構造が変わることは、このsliceで明示的に受け入れる。

### `AgentRuntime`

`Agent`と型付きinputを、検証済み`OutputT`へ変換する1 attemptの実行境界。

provider-neutralなProtocolは、概念上次の契約を持つ。

```python
async def invoke(
    self,
    agent: Agent[InputT, OutputT],
    input: InputT,
    *,
    attempt_number: int,
) -> OutputT: ...
```

client lifecycleをRuntimeへ混ぜずにphase scopeを注入するため、同じmoduleに次のprovider-neutralな
callable contractを置く。

```python
class AgentRuntimeScopeFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AgentRuntime]: ...
```

factory実装はcompositionに置き、Serviceは具象client型やclose APIを知らない。

`attempt_number`は1以上とし、phase policy ownerである呼び出し側が決める。Plannerは1または2を渡す。
Runtimeは番号を保持・推測しない。この値はtraceの識別にだけ使い、provider request、retry可否、
model outputを変えない。

- provider clientを使って1回だけrequestする。
- `agent.model.provider`が`"gemini"`でない場合は誤配線として、renderer、config構築、attempt span、
  provider requestより前に拒否する。
- provider clientは呼び出し側のscopeから借り、成功・失敗にかかわらず`invoke()`内でcloseしない。
- 固定instructionsとrender済みtask inputをprovider固有のmessage構造へ変換する。
- `ModelSettings`をprovider SDK設定へ変換し、`None`のfieldをrequestから除外する。
- Geminiの`response_mime_type`等、structured outputを実現するprovider固有設定を適用する。
- provider responseをoutput typeへparse / validateする。
- provider固有errorを既存の共通errorへ変換する。
- provider attempt spanを開き、そのspan durationをlatencyの正本とする。
- provider responseにusageがあれば、blocked判定、JSON parse、schema validationより前にattempt spanへ記録する。
- provider call自体がresponseを返さず分類済みprovider errorになった場合は`result="provider_error"`、
  error、span durationを記録する。responseがないためusageは捏造しない。未分類例外では`result`を
  捏造せず、errorとspan durationだけを記録する。

Planner固有のretry / fallback policyは知らない。
成功時は`OutputT`だけを返す。usageはobservabilityのためにspanへ記録する値であり、コードが判断に使う
consumerが存在しないPR1では、`AgentAttemptResult`、usage accumulator、usage付き例外を追加しない。
attempt spanをusage attributeの唯一の正本とし、phase / run spanへusageを複写しない。後続observability
sliceで集約表示やmetric consumerを追加する場合も、attempt記録から導出し、phase / run spanへの複写や
戻り値への同乗によって同じusageを二重計上しない。

### Invalid response contract

provider responseを`OutputT`へ変換できない失敗は、`runtime/contract.py`に置くprovider-neutralな共通契約で
表現する。

```python
class AgentResponseDefect(StrEnum):
    RESPONSE_NOT_JSON = "response_not_json"
    RESPONSE_NOT_OBJECT = "response_not_object"
    OUTPUT_SCHEMA_MISMATCH = "output_schema_mismatch"


class AgentResponseInvalidError(ValueError):
    def __init__(
        self,
        defect: AgentResponseDefect,
        *,
        repair_hint: str | None = None,
    ) -> None: ...
```

各defectの境界は次のとおり。

- JSONとしてdecodeできない: `RESPONSE_NOT_JSON`
- JSONとしてdecodeできるがrootがobjectでない: `RESPONSE_NOT_OBJECT`
- objectを`agent.output_type`でvalidateできない: `OUTPUT_SCHEMA_MISMATCH`

`str(error)`はdefect値と`repair_hint`だけから決定的に構成し、次attemptの`previous_error`としてそのまま
sanitize境界へ渡せる安全な文字列にする。`repair_hint`は修正に必要なfield path、Pydantic error type、
明示的にallowlistした制約値だけから構成する。`ValidationError.errors(include_input=False)`の結果もそのまま
文字列化しない。`loc`の文字列は`agent.output_type`が宣言したfield名 / validation aliasだけを許可し、
数値indexは許可する。extra field、dict key等のmodel出力由来となり得る未知componentは固定placeholderへ
畳む。`type`と安全と確認した`ctx` keyだけを明示的に選び、生の`input`、provider response、自由文`msg`、
`url`、任意の`ctx`を含めない。invalid JSON / non-objectではraw responseをdetail化せず、固定の安全な
repair hintだけを使う。

`JSONDecodeError` / `ValidationError`から安全な分類済み例外へ変換するときは、変換元例外を保持したまま
`raise ... from None`するだけでは不十分である。安全なdefect / repair hintを`except`内で抽出し、
`except` scopeを退出してから`AgentResponseInvalidError`をraiseすることで、raw response / inputを持つ例外を
`__context__` / `__cause__`へ接続しない。

`QuestionPlanningService`は`AgentResponseInvalidError`の3 defectだけをresponse-shape failureとして
request内で最大1回retryする。auditへの写像は次のとおり。

- `code = error.defect.value`
- `failure_kind = "ai_response_invalid"`
- `failure_reason = error.defect.value`
- `request_retry_disposition = "retry_in_request"`

blocked outputと分類済みprovider errorは既存どおりrequest内retryを行わず、未分類例外はfallbackへ変換せず
伝播する。Runtime内部のPydantic `ValidationError`をService境界へ直接漏らさず、
`OUTPUT_SCHEMA_MISMATCH`へ変換する。旧`QuestionPlannerResponseInvalidError`、Planner / Gemini名を含む旧defect、
Pydantic validation用の別audit codeは削除し、互換aliasを残さない。

### Trace contract

Plannerのlogical phaseと実provider requestを、次の2階層で分ける。

```text
agent_answering_run {run_id}
└─ agent_phase {phase="question_planning", agent_name="question_planner"}
   ├─ agent_provider_call {agent_name="question_planner", attempt_number=1,
   │                       prompt_version=<agent prompt version>,
   │                       gen_ai.operation.name="generate_content",
   │                       gen_ai.provider.name="gcp.gemini",
   │                       gen_ai.request.model=<configured model>}
   └─ agent_provider_call {agent_name="question_planner", attempt_number=2, ...}  # retry時だけ
```

`agent_phase`はPlanner phase policy全体を囲み、retryしても増やさない。PR1では既存の
`QuestionPlanningService.plan()`境界で開き、outer `agent_answering_run`との親子関係はambient trace
contextで接続する。後続でphase policyの配置を`AnsweringRunner`へ移す場合も、このspan契約ごと移す。

`agent_phase`の独自attribute allowlistは次の2つとする。

- `phase="question_planning"`
- `agent_name="question_planner"`

`agent_provider_call`は`GeminiAgentRuntime`が実provider requestごとに1本、SpanKind `CLIENT`として開く。
rendererとprovider configの構築はspan開始前に完了し、provider requestを送らない失敗経路では
`agent_provider_call`を作らない。provider responseのusage / finish reason取得とJSON / output type validationを
終えて`result`を分類してからspanを閉じる。

`agent_provider_call`を閉じた時点の独自attribute allowlistは次のとおり。

- `agent_name`: Agent宣言のstable name
- `attempt_number`: 呼び出し側が渡した1以上の整数。Plannerでは`1 | 2`
- `prompt_version`: Agent宣言の固定Prompt version
- `result`: Runtimeが結果を分類できた場合だけ記録する

Logfire / OpenTelemetryが自動付与する内部attributeはアプリケーション独自attributeのallowlist比較対象外とし、
アプリケーションが明示的に設定した独自attributeだけを上記集合と照合する。
ロック版Logfire 4.37は`gen_ai.request.model`があるspanへ`gen_ai.response.model`をexport時に自動補完する。
これはframeworkが導出するGenAI標準attributeとして同じく独自allowlist比較対象外とする。Runtimeは
`gen_ai.response.model`を明示設定せず、provider responseから実際のmodel値を取得したものとしても扱わない。

GenAI標準attributeは次のとおり。Gemini Developer APIを使うPR1では、内部の
`ModelTarget.provider="gemini"`を標準値`gcp.gemini`へRuntimeが写像する。将来Vertex AIを使う場合は
`gcp.vertex_ai`とし、同じ値を流用しない。

- `gen_ai.operation.name="generate_content"`
- `gen_ai.provider.name="gcp.gemini"`
- `gen_ai.request.model`: Agent宣言にある固定model名
- `gen_ai.response.model`: Logfire 4.37がrequest modelから自動補完する場合だけ存在するframework導出値

Runtimeが結果を分類できた場合だけ、`result`へ`succeeded | blocked | invalid_response | provider_error`の
いずれかを記録する。分類済みerrorでは標準`error.type`へlow-cardinalityな識別子を記録し、span statusを
descriptionなしの`ERROR`にしたうえでspanを閉じ、その後に例外をraiseする。分類済みerrorを
`agent_provider_call` / `agent_phase`のexception eventとして記録しない。未分類例外では`result`を捏造せず、
例外をspanから伝播させて既存のexception event / error記録を維持する。未分類例外のmessage、stacktrace、
Pydantic失敗入力、status descriptionは、既存の`ExceptionRedactingProcessor`がexport境界で
`[redacted]`化する。error本文は独自attributeへ転記しない。

`ExceptionRedactingProcessor`の対象はspan exportだけであり、structlogのstdout / LogRecordは保護しない。
回答workerの最終unexpected handlerは`logger.exception()`を使わず、固定event、`run_id`、例外class名だけを
`logger.error()`で記録する。これにより未分類provider例外とPlanner client close例外の自由文をstdoutへ
シリアライズしない。stacktraceを安全に保持する共通logging processorは本sliceでは導入しない。

provider responseにusageが存在するときだけ、provider固有名を次のGenAI標準数値attributeへ写像する。
存在しない値を`0`で補わない。

- `gen_ai.usage.input_tokens`
- `gen_ai.usage.output_tokens`
- `gen_ai.usage.cache_read.input_tokens`
- `gen_ai.usage.reasoning.output_tokens`

上記2つの内訳attributeは現行OpenTelemetry GenAI registryの綴りを採用する。実装時にlock済み
semantic-conventions packageで対応する定数が公開されているか、Logfire panelが集計表示するかを照合する。
定数や特別なpanel表示がない場合も標準attribute自体は同じ綴りで記録し、独自の代替キーを作らない。

total tokenは独自attributeへ複写せず、input / output等の標準attributeから観測側で導出する。
latencyの正本は`agent_provider_call`のspan durationとし、`latency_ms`等の重複attributeを追加しない。
`run_id`は親`agent_answering_run`だけに置き、child spanへ複写しない。question、history、instructions、
render済みinput、requirements、`previous_error`、provider response、draft、completed plan、final answerを
phase / attempt spanへ記録しない。Prompt versionはprovider attemptにだけ固定metadataとして記録し、
phaseへ複写しない。

### Composition

Agent宣言、Gemini-backed runtime、client、運用設定を配線する。rate limit policyはAgentの役割宣言へ
含めない。このsliceでは既存policyを新たにconsumeせず、Agent経路へのrate limit enforcementは
別sliceで決める。

Planner用Gemini async clientの生成と破棄はcompositionが実装するscope factoryへ集約する。
factoryは呼び出されるまでclientを作らず、SDKのasync context managerで取得したclientを
`GeminiAgentRuntime`へ貸し出す。scope退出時のcloseはfactoryだけが所有する。
`google.genai`、Gemini error translator、`GeminiAgentRuntime`のimportもscope activationまで遅延し、
非AIのAPI / scheduler processがcomposition moduleをimportしただけでprovider SDKをloadしない。

```python
@asynccontextmanager
async def activate_gemini_agent_runtime() -> AsyncIterator[AgentRuntime]:
    async with genai.Client(api_key=...).aio as client:
        yield GeminiAgentRuntime(client=client)
```

PR1ではこのscopeをplanning phaseだけに限定する。workerから`AnsweringRunner.run()`全体を囲まず、
Question Context、Direct / Evidence Answer、internal embeddingの既存clientへ共有しない。
Gemini clientの明示的なconnection pool設定も追加せず、SDK既定値を使う。
close自体が失敗した場合は独自にcatch / retry / suppress / combineせず、close例外が本体例外を置き換え得て
元の例外が`__context__`に残るPython / SDK既定挙動をPR1の受容済みリスクとする。

### Prompt declaration and version

Prompt versionは、固定Prompt本文のrevisionを記録したいconsumerが`agent.prompt.version`として参照する
Prompt固有metadataである。Runtimeの戻り値やprovider requestへ含めず、provider attempt spanにだけ
`prompt_version`として記録し、phase spanへ複写しない。

versionの値と固定本文を同じ編集視界へ置くため、role固有`prompts.py`を正本とする。

```python
# backend/app/agent/planning/prompts.py
PLANNER_PROMPT_VERSION: Final[str] = "v1"
PLANNER_INSTRUCTIONS: Final[str] = "..."
_PLANNER_INPUT_TEMPLATE: Final[str] = "..."


def render_planning_input(input: PlanningAttemptInput) -> str:
    ...
```

`planning/agent.py`はversionを再定義せず、固定Prompt側の値を参照して宣言を組み立てる。

```python
# backend/app/agent/planning/agent.py
QUESTION_PLANNER_PROMPT = AgentPrompt(
    version=PLANNER_PROMPT_VERSION,
    instructions=PLANNER_INSTRUCTIONS,
    input_renderer=render_planning_input,
)

QUESTION_PLANNER_AGENT = Agent(
    name="question_planner",
    prompt=QUESTION_PLANNER_PROMPT,
    ...,
)
```

`PLANNER_PROMPT_VERSION`は手動で更新するopaqueなrevisionとする。旧`compute_call_signature()`、旧hash値、
旧audit recordとの互換性は持ち込まない。Question Plannerのaudit記録は、旧adapterの`prompt_version`
propertyや`getattr` fallbackではなく、新しいAgent宣言の`agent.prompt.version`を直接参照する。modelは
`agent.model.name`を別metadataとして参照し、modelやsettingsの変更をPrompt versionへ混ぜない。

### `QuestionPlanningService`

Planner phaseのdomain policy owner。

Serviceは旧`QuestionPlanDraftGenerator` adapterを1つ受け取る形から、Planner `Agent`宣言と、
compositionが実装するprovider-neutralなRuntime scope factoryを別々に受け取る形へ変更する。

```python
class QuestionPlanningService:
    def __init__(
        self,
        *,
        agent: Agent[PlanningAttemptInput, QuestionPlanDraft],
        runtime_scope_factory: AgentRuntimeScopeFactory,
        audit_recorder: PlannerAuditRecorder | None = None,
    ) -> None: ...
```

`plan()`はagent phase内で`async with self._runtime_scope_factory() as runtime`を1回だけ開始し、
attempt実行を`runtime.invoke(self._agent, attempt_input, attempt_number=n)`とする。attempt 2へretryしても
scopeを開き直さず、同じRuntime / clientを使う。auditへ記録するmodelとPrompt versionはそれぞれ
`self._agent.model.name`、`self._agent.prompt.version`から直接取得し、旧Planner adapterの
`model_name` / `prompt_version` propertyや`getattr` fallbackは残さない。

- attempt番号と`previous_error`を管理する。
- response-shape failureだけを既存条件でretryする。
- retry不能または上限到達時にsafe fallbackを作る。
- `QuestionPlanDraft`をcompleted `QuestionPlan`へ変換する。
- auditとoutcome metricsを記録する。

### Gemini-backed runtime

借りたGemini async client、`generate_content`、finish reason、response envelope、Gemini error translationを
所有する。
`agent.prompt.instructions -> system_instruction`、render済みinput `-> contents`の変換とusage記録も所有する。
Plannerのretry / fallbackやworkflow分岐、clientの生成・closeは所有しない。

## 実行フロー

```text
QuestionPlanningService.plan(request)
└─ agent_phaseを1本開く
   └─ async with runtime_scope_factory()  # planning phaseで1 scope
      └─ attempt loop（最大2回、同じRuntime / client）
         ├─ PlanningAttemptInput(request, previous_error)を作る
         ├─ AgentRuntime.invoke(planner_agent, input, attempt_number=n)  # 1 provider attempt
         │  ├─ agent.prompt.input_renderer(input)でmodel-visibleなtask inputを作る
         │  ├─ agent_provider_call CLIENT spanを1本開く
         │  ├─ instructionsとtask inputをproviderの別message fieldへ渡す
         │  ├─ responseにusageがあればattempt spanへ記録する
         │  └─ QuestionPlanDraftを直接返す、または既存分類の例外を送出する
         ├─ 成功: plan_from_draft() -> completed QuestionPlan
         ├─ retry可能: previous_errorを設定して次attemptへ進む
         └─ retry不能 / 上限到達: safe_fallback_plan()
      # scope退出時にcompositionがasync clientのcloseを1回試行
```

移行中の上位フローは次のまま維持する。

```text
AnsweringRunner
└─ starting_agent
   └─ QuestionAnsweringOrchestrator
      └─ QuestionPlanningService
         └─ Planner Agent + AgentRuntime
```

## Test contract

- `AgentPrompt`とAgent宣言がfrozen / statelessで、Agentがstable name、Prompt、model、output typeを持つ。
- Agent宣言が`AgentPrompt`、`ModelTarget`、型付き`ModelSettings`、`response_schema`を持ち、
  rate limit policyを持たない。
- `AgentPrompt.version`が空でない明示revision、instructionsが固定`str`、input rendererが同期Callableとして
  宣言される。
- `PLANNER_PROMPT_VERSION`が固定instructions / input templateと同じ`planning/prompts.py`にあり、
  `QUESTION_PLANNER_PROMPT.version`がその定数を参照する。`planning/agent.py`へversion literalを複製しない。
- Planner auditへ記録するPrompt versionは`agent.prompt.version`、modelは`agent.model.name`から取得し、
  旧Planner adapterの属性探索を残さない。
- 新しいPlanner Agent / Prompt宣言が旧`compute_call_signature()`へ依存しない。
- 固定の役割・判断基準・出力ルールがinstructionsだけにあり、input rendererへ複製されない。
- input rendererが`PlanningAttemptInput`だけから決定的にmodel-visibleなtask inputを作り、
  sanitize境界を維持する。
- retryの固定修正ルールはinstructionsに1回だけ存在し、初回contentsにprevious error sectionを含めず、
  retry contentsにはsanitize済み`previous_error`値だけを追加する。
- input rendererがI/Oを行わず、外部状態を暗黙に取得しない。
- Planner Agentが共通`Agent` dataclassのinstanceで、role固有subclassを必要としない。
- `GeminiAgentRuntime`のconstructorがGemini clientだけを受け取り、Agent宣言やretry stateを保持しない。
- `app.agent.runtime.contract`だけをimportしたときにGemini具象module / SDKを暗黙にloadせず、package
  `__init__`がprovider-neutralなcontractだけを再公開する。
- compositionのRuntime scope factoryが呼ばれるまでGemini clientを生成せず、1 planning phaseにつき
  factoryを1回だけactivateする。
- clean processでcomposition moduleをimportし、Planner Runtime scopeを構築しただけでは`google.genai`と
  Planner用Gemini具象Runtimeをloadせず、scope activation時だけloadすることを検証する。
  `AnsweringRunner`と既存の他Gemini工程を含む回答graph構築後までSDK未loadであることは要求しない。
- first attempt成功とretry成功の双方でscopeを1回だけ退出し、retry時はattempt 1と2が同じRuntime / async
  client identityを使う。
- 同じServiceの`plan()`を2回呼ぶとscope factoryが2回activateされ、planning phase間で異なるRuntime /
  async client identityを使う。
- provider / response failure、想定外例外、task cancellation、client取得後のRuntime構築失敗で、
  closeが正常終了するfake async clientを1回だけcloseし、元の終了経路を抑止しない。
- `GeminiAgentRuntime.invoke()`を単体で呼んでも借りたclientをcloseせず、close ownerがcompositionの
  scope factoryだけである。
- context preparationまたはhookで短絡した既存上位flowではPlanner Runtime scopeをactivateしない。
- Gemini clientへ独自のconnection pool上限、keep-alive tuning、回答Run間の共有を追加しない。
- 同じ`GeminiAgentRuntime`へ異なるAgent宣言を順番に渡しても、各invokeが渡されたAgent Promptのinstructions、
  model settings、schemaだけを使い、前回の宣言・input・usageを引き継がない。
- runtime 1 invocationにつきprovider callは1回である。
- 呼び出し側が`attempt_number`を明示し、Runtimeがその番号からretry policyや状態を推測しない。
- providerが`gemini`でないAgentはrenderer / config / span / provider callより前に拒否する。
- runtimeは成功時に`QuestionPlanDraft`を直接返し、usage用result envelopeを返さない。
- Runtimeは`output_type`をPydantic `TypeAdapter`で検証し、`BaseModel`だけでなく宣言されたdataclass等の
  対応型を検証済み`OutputT`として返す。
- runtimeがretry / fallback / 別Agent / Toolを起動しない。
- first attempt成功時、runtime 1回で既存completed planと同値になる。
- response-shape failure後の成功時、runtime 2回、retry audit / metricsが既存と同じ条件・回数で発火する。
- retry不能failureまたは2回失敗時、既存safe fallbackとfailure auditの発火条件を維持する。
- invalid JSON、non-object、blocked output、Pydantic validation、provider errorのretry / fallback上の意味を
  維持する。例外名とaudit code値の一致は要求しない。
- invalid JSON / non-object / Pydantic validationを、それぞれ`RESPONSE_NOT_JSON` /
  `RESPONSE_NOT_OBJECT` / `OUTPUT_SCHEMA_MISMATCH`として共通例外へ変換し、3分類だけが最大1回retryされる。
- invalid response auditの`code` / `failure_reason`がdefect値、`failure_kind`が`ai_response_invalid`、
  dispositionが`retry_in_request`であり、生のrepair hintやmodel outputをauditへ記録しない。
- `OUTPUT_SCHEMA_MISMATCH`の`repair_hint`と`str(error)`にfield path、error type、allowlist済み制約値だけが
  含まれ、生のPydantic input、provider response、自由文`msg`、`url`、任意の`ctx`、入力由来sentinelが
  含まれない。extra fieldやdict keyの任意文字列はfield pathへ反映せず固定placeholderへ畳む。
- invalid JSONと`OUTPUT_SCHEMA_MISMATCH`から変換した分類済み例外の`__context__` / `__cause__`を含む
  exception graph全体に、生のprovider response、Pydantic input、入力由来sentinelが残らない。
- model settingsからGemini requestを作る際に`None`を除外し、既存の有効request設定を維持する。
- Gemini requestの`system_instruction`には`agent.prompt.instructions`だけ、`contents`には
  `agent.prompt.input_renderer(input)`でrenderしたtask inputだけを渡す。
- responseにusageがある成功、blocked、invalid JSON / schema経路では、各attempt spanへusageを1回記録する。
- provider callがresponseを返さず分類済みprovider errorになった経路ではusageを記録せず、
  `result="provider_error"`、error、span durationを記録する。未分類例外では`result`を記録しない。
- first attempt成功時は`agent_phase`が1本、その直下の`agent_provider_call` CLIENT spanが1本である。
- retry時は同じ`agent_phase`が1本のまま、その直下の`agent_provider_call`がattempt番号1、2で2本になる。
- `agent_provider_call`の`gen_ai.operation.name="generate_content"`、
  `gen_ai.provider.name="gcp.gemini"`、`gen_ai.request.model`、Agent宣言と一致する`prompt_version`を検証する。
- `agent_phase`の最終独自attribute keyが`phase` / `agent_name`、`agent_provider_call`が`agent_name` /
  `attempt_number` / `prompt_version` / 条件付き`result`だけであることを検証する。Logfire / OpenTelemetry内部attributeは
  アプリケーション独自attributeの比較対象外とする。Logfire 4.37が自動補完する
  `gen_ai.response.model`もframework導出の標準attributeとして比較対象外とし、Runtimeが明示設定しない。
- attribute、exceptionを含むevent、status descriptionを含むspan全面を検査し、入力由来sentinel、
  model-visible text、childへの`run_id`複写がなく、`prompt_version`はprovider attemptだけにある。
- usageが存在するfieldだけ`gen_ai.usage.*`へ写像され、欠損値を`0`で補わず、独自total token属性を作らず、
  latencyの正本がspan durationである。
- `OUTPUT_SCHEMA_MISMATCH`を含む分類済みerrorでは`result`、標準`error.type`、descriptionなしのERROR statusを
  記録し、`agent_provider_call` / `agent_phase`にexception eventを作らずspanを閉じた後にraiseする。
- 未分類例外ではexception eventを維持し、export後のmessage、stacktrace、Pydantic失敗入力、
  status descriptionが`[redacted]`で、span全面にmodel-visibleなsentinelがない。
- 回答workerの未分類例外とPlanner client close失敗を扱う最終logがerror level、固定event、`run_id`、
  `error_type`だけを持ち、production相当のstdout JSONへ`exc_info`、例外message、stacktrace、入力由来sentinelを
  出力しない。
- Service、Runner、`RunResult`へusageを返さず、phase / run spanへusage attributeを追加しない。
- response schemaと`QuestionPlanDraft`の別契約が、field / required / enum / nullable / type / representative
  payloadの整合性testで守られる。
- Agent宣言後にresponse schemaを変更できない。
- Prompt versionの正本が`AgentPrompt`のmetadataにあり、Runtime戻り値やprovider requestへ混入せず、
  provider attempt spanだけがその値を記録する。
- 旧prompt version / call signatureの互換経路を残さず、旧audit code / version値との連続性をtestしない。
- rate limitの新規適用を行わない。
- attempt spanにmodel-visible textを含めない。
- planning、更新後audit契約、prompt/schema、Gemini runtime、orchestration regressionを通す。

### 実装PRの確認手順

- 代表的な初回attemptとretry attemptについて、Geminiへ渡す`system_instruction`と`contents`のgoldenを
  人間が一読し、固定instructionsとtask inputの分離、repair情報の過不足、model-visible textを確認する。

## 実装前の決定状況

Agent / Prompt / Runtimeの主要責任は確定した。

- `AgentPrompt`とAgentの責任、Prompt versionの宣言場所、instructions / inputの物理分離。
- Runtimeの直接`OutputT`返却と、usageを戻り値へ運ばない方針。
- `AgentRuntime` / `GeminiAgentRuntime`の名前、配置、clientだけを保持するinstance責任。
- required keywordの`attempt_number`と、retry stateをRuntimeへ持たせない方針。
- 固定名`agent_provider_call`、SpanKind `CLIENT`、GenAI標準attribute、独自attribute allowlist、
  renderer失敗時にspanを作らないGeneration span契約。
- composition所有の`AgentRuntimeScopeFactory`をplanning phaseで1回activateし、最大2 attemptで同じGemini
  async clientを共有した後、正常・例外・cancelの各経路でcloseを1回試行するclient lifecycle契約。
- PR1では回答Run全体のGemini client共有とconnection pool tuningを行わない方針。

Runtimeのinvalid response契約も確定した。

- provider-neutralな`AgentResponseInvalidError`と3つの`AgentResponseDefect`を使う。
- repair情報はfield path、error type、allowlist済み制約値だけから構成する。
- `QuestionPlanningService`は3 defectだけをrequest内でretryし、defect値をauditのcode / failure reasonへ使う。
- blocked / provider errorはrequest内retryせず、未分類例外は伝播する。

以上により、本sliceの実装開始を止める未決定事項はない。

実装時に新たな責任判断が必要になった場合は、本仕様を暗黙に拡張せず、Problem / Invariants / Non-goals /
Doneとの差を確認して仕様へ戻る。
