# Agent 宣言と AnsweringRunner orchestration 責任仕様

更新日: 2026-07-18

実装状況: Planned

## 位置付け

本仕様は、実装済みの `agent-answering-runner-boundary-slice.md` を前提として、
Vector の回答処理における `Run`、`Runner`、`Agent`、`Tool` の責任を確定する。

先行 slice では、ユーザー質問と bounded history から回答 context を準備し、
`starting_agent.answer()` を1回呼び、`RunResult` を返す外側の実行境界を抽出した。
その結果、1回の回答処理を `agent_answering_run` span で囲めるようになった一方、
planning、検索、回答生成の実際の順序は `QuestionAnsweringOrchestrator` と
`ExternalSearchResearchRunner` の内側に残っている。

本仕様では、1つの Agent に回答 workflow 全体を隠さない。ユーザー入力から最終出力までの
処理順と分岐を `AnsweringRunner` が所有し、各 Agent は1つの LLM 役割、各 Tool は1つの
実行能力として扱う。

本仕様は責任と移行順を固定する設計仕様である。PR1の詳細仕様
`planner-agent-runtime-slice.md`では、`AgentPrompt`と`Agent`をfrozen dataclassとし、Prompt version、
固定instructions、同期`input_renderer`を`AgentPrompt`へ関連づける。Prompt versionの正本は固定本文と同じ
role固有`prompts.py`へ置き、Agent組み立て側は参照するだけとする。PR1ではversionを手動revisionとし、
固定instructions、固定input template、`response_schema`のmodel-visibleな内容を変えるときに更新する。
model指定を`ModelTarget` /
`ModelSettings`、出力境界を`output_type` / `response_schema`とする。固定instructionsと実行時task inputは
provider request上でも分離し、`AgentRuntime.invoke()`は検証済み`OutputT`だけを返す。usageはruntimeが
attempt spanへ記録し、戻り値へ含めない。旧prompt call signature機構は持ち込まず、rate limit policyは
Agent fieldへ含めない。provider-neutral portを`AgentRuntime`、Gemini実装を
`GeminiAgentRuntime`とし、`agent/runtime/`へ配置する。Agent phaseの固定span名は`agent_phase`を維持し、
provider generation attemptは固定名`agent_provider_call`の`CLIENT` spanとする。API操作、provider、model、
usageは標準`gen_ai.*` attributeへ記録する。

前提仕様:

- `backend/specs/agent-answering-runner-boundary-slice.md`
- `backend/specs/agent-question-context-preparation-slice.md`
- `backend/specs/question-answering-retrieval-orchestration-slice.md`
- `backend/specs/question-answering-external-research-task-contract-slice.md`
- `backend/specs/external-search-research-runner-slice.md`
- `backend/specs/external-search-deepseek-adapters-slice.md`
- `backend/specs/external-search-tavily-provider-slice.md`

### 先行仕様からの更新

`agent-answering-runner-boundary-slice.md` は、Runner境界を安全に抽出する最初のsliceとして、
direct / internal / external / mixed分岐を `QuestionAnsweringOrchestrator` に残すこと、
`ExternalSearchResearchRunner` を既存の部分workflowとして維持すること、workflow全体を
Runnerへ移す変更をnon-goalとしていた。

それらは抽出slice内の移行制約として正しい。本仕様はその実装済み境界を前提に、次の設計段階で
責任を再配置する後続仕様であり、将来形について次を上書きする。

- 回答workflowの最終ownerを `QuestionAnsweringOrchestrator` ではなく `AnsweringRunner` とする。
- `ExternalSearchResearchRunner` をnested Runnerとして維持せず、Agent、Tool、Runner policyへ分解する。
- workflow全体を表す `QuestionAnsweringAgent` をstarting agentとして維持せず、役割別Agentへ分解する。

同様に、`external-search-research-runner-slice.md` が実装sliceとして固定した
`ExternalSearchResearchRunner` のowner責任と `ExternalSearchRunResult` 等の命名は、移行中の
互換seamとしてだけ維持し、最終形については本仕様が上書きする。

先行仕様のDB / worker境界、context同一性、resource cleanup、error propagation、trace / PII制約は
上書きせず、本仕様でも維持する。

## Work Definition

### Problem

1. 現在の `Runner.run()` から見える回答処理は context preparation、hook、
   `starting_agent.answer()`、`RunResult` 構築までであり、どの Agent と検索能力が
   どの順序で起動するかを読み取れない。
2. `QuestionAnsweringAgent` は `answer()` だけの実行 port であり、name、instructions、
   model、output type を宣言する Agent ではない。
3. `QuestionAnsweringOrchestrator` が planning、direct / retrieval 分岐、evidence collection、
   answer assembly を所有し、回答 workflow の本体が Runner とは別概念になっている。
4. `ExternalSearchResearchRunner` が query 生成、検索 provider 呼び出し、候補 pool 構築、
   evidence 選別、task 並列制御をまとめて所有し、Agent と Tool の境界を隠している。
5. `ExternalSearchResearchRunner`、`ExternalSearchRunResult`、`ExternalSearchRunner` など、
   ユーザー入力から最終出力までを表さない部分処理にも `Run` / `Runner` が使われている。
6. DeepSeek function calling は structured output を取得するために使われているが、
   実行可能な検索 Tool と同じ語彙で説明すると、model が検索 Tool を選択しているように見える。
7. Agent 宣言の前に workflow 全体を1つの Agent として包むと、複数の prompt、model、
   output schema、provider を持つ orchestration object が Agent と呼ばれ、責任が曖昧になる。

### Evidence

- `backend/app/agent/running/runner.py::Runner.run()` は context preparation と hook の後、
  `QuestionAnsweringAgent.answer()` を1回だけ呼ぶ。
- `backend/app/agent/contract.py::QuestionAnsweringAgent` は
  `answer(AnswerQuestionInput) -> AnswerQuestionResult` だけを公開する Protocol である。
- `backend/app/agent/answering/orchestration.py::QuestionAnsweringOrchestrator.answer()` は
  planner を呼び、`RetrievalPlan` を Python `match` で分岐し、direct answer または
  evidence collection と evidence answer を進行する。
- `backend/app/agent/evidence_collection/service.py::EvidenceCollectionService.collect()` は
  internal / external / mixed retrieval の分岐と mixed retrieval の並列実行を所有する。
- `backend/app/agent/evidence_collection/external_search/runner.py` は task ごとに
  `QueryGenerator`、`SearchProvider`、`EvidenceSelector` を順番に呼ぶ。
- `DeepSeekQueryGenerator` と `DeepSeekEvidenceSelector` はそれぞれ独立した prompt、model spec、
  output schema、typed error を持つ LLM 役割である。
- `TavilySearchProvider.search()` は完成済み query を受け、外部 HTTP 検索と response 正規化だけを
  行い、query の生成や evidence の選別を行わない。
- Postgres の `AgentRun` は1つの user turn の persistent lifecycle を表し、worker が attempt、
  完了・失敗永続化、terminal 通知を所有する。
- `agent_answering_run` span は context preparation から final output 取得までを包含し、
  DB 完了永続化と terminal 通知は span の外側にある。

### Invariants

#### Run と Runner

- `Run` は1つのユーザー入力を受け取ってから、その入力に対する最終 `RunResult` または
  terminal exception / stop / cancelへ到達するまでのlogical turnだけを表す。
- 1つの persistent `AgentRun` attempt に対して、回答用 `AnsweringRunner.run()` invocation は
  最大1つとする。
- Agent 呼び出し、Tool 呼び出し、外部検索 task、query 単位の検索を子 `Run` と呼ばない。
- `AnsweringRunner.run()` の内側から別の `Runner.run()` を再帰的・階層的に呼ばない。
- 部分処理を private method や capability object に分割しても、それを新しい Run と扱わない。
- `RunContext.run_id` と `as_of` は回答 Run 全体で1つだけ生成し、Agent / Tool 呼び出しごとに
  新しい `RunContext` を作らない。
- 各child invocationは同じ親trace contextに属する。ただし、`RunContext` 全体を各入力へ渡さず、
  `as_of` などは時制判断に必要なAgent / phaseだけへ型付きinputとして投影する。
- `AnsweringRunner` は DB、SQLAlchemy、Taskiq、Redis、HTTP client、transaction、
  persistent completion、terminal 通知を知らない。
- worker は persistent attempt 取得、bounded history query、error mapping、完了・失敗永続化、
  terminal 通知を引き続き所有する。

#### Workflow ownership

- ユーザー入力から final output までの Agent / Tool の起動順と分岐は
  `AnsweringRunner` が所有する。
- 回答 workflow 全体を単一の Agent 定義または `starting_agent.answer()` の内側に隠さない。
- `QuestionAnsweringOrchestrator` が現在所有する planning、direct / retrieval 分岐、
  evidence collection、answer assembly は、移行完了時に `AnsweringRunner` の責任となる。
- Agentは別のAgentまたはToolを直接起動しない。初期設計では`AnsweringRunner`だけが
  Agent phase / attemptとTool callを進行する。
- model が自由に Tool や次の Agent を選択する agent loop は導入せず、Python の明示的な
  branching を維持する。
- internal / external / mixed retrieval の既存結果、partial failure、missing aspects、
  source assembly の意味を変更しない。
- 読みやすさのため workflow の一部を private method に分ける場合も、`run()` から
  planning、retrieval、synthesis の高レベル順序を追えるようにする。

#### Agent

- 1つの Agent は1つの LLM 役割を表す。
- Agent 宣言は immutable / stateless とし、user input、history、`RunContext`、provider client、
  DB session、reporter、生成途中の状態を保持しない。
- Agent 宣言は少なくともstable name、`AgentPrompt`、model設定の正本、output type / schemaの正本を
  関連づける。
- `AgentPrompt`はversion、固定instructions、同期input rendererを関連づける。instructionsは固定の役割・
  判断基準・出力ルールをすべて持ち、input rendererは実行時task inputだけを作る。同じ固定ルールを
  両方へ複製しない。
- PR1で導入する初期Agent契約では、Prompt versionは固定本文を所有するrole固有`prompts.py`に本文と隣接した
  手動revisionとして明示し、Agent組み立て側はその定数を参照する。固定instructions、固定input template、
  `response_schema`のmodel-visibleな構造、enum、required、descriptionを変えるときにversionも更新する。
  `output_type`だけの変更、model、model settings、実行時inputの変更では更新せず、旧call signatureや
  content hashから自動算出しない。
- rate limit policyをAgent fieldへ含めない。
- instructions、model、output schema を旧 spec と新 Agent 宣言の両方へ複製しない。
- Agent の local invocation context 全体を model input へ暗黙に serialize しない。
- 各 Agent には、その役割が必要とする入力だけを型付き input として投影する。
- Agent output は役割固有の型として validation し、Runner は未検証 dict を受け取らない。
- Query / Selector Agentへ渡すresearch goal、title、snippetは既存のsanitize、長さ上限、
  untrusted-data境界を維持し、generic context serializationでpromptへ展開しない。
- Selector Agentのmodel-visible candidateにはURLを含めず、source identityはoutput内の
  candidate indexだけで指定する。claim、why selected、missingは既存schemaどおりmodel outputに
  含められるが、範囲外・重複indexをdropし、URL、title、snippet等のsource metadataはRunnerが
  Tool由来candidate poolから再構築する。
- 初期 Agent 宣言に未使用の handoffs、guardrails、session、max turns、generic tool loop を
  追加しない。
- model が実行可能な Tool を直接選択しない初期設計では、`Agent`へ`tools` fieldを
  形だけで追加しない。Tool は `AnsweringRunner` の capability として配線する。

#### Tool

- Tool は外部 I/Oや検索などの実行可能な単一能力を表す。本仕様でTool化を確定する対象は
  External Search Toolであり、internal retrievalの最終分類と命名は後続sliceで決める。
- External Search Tool は完成済み query を入力として受け、query の目的や文字列を生成しない。
- External Search Tool は検索結果の回答適合性を判断せず、candidate の provider response を
  安全な内部型へ正規化して返す。
- External Search Query Agentが何をどの文字列で検索するかを決め、External Search Toolがそのqueryを
  書き換えず実行する。
- EvidenceSelector Agent が検索 candidate から回答根拠を選び、External Search Tool は
  evidence selection を所有しない。
- HTTP transport / request timeout、HTTP error translation、response parsing、安全なURL validationは
  External Search Tool adapterの責任とする。
- Tool call全体に対するworkflow backstop timeoutと、query単位のtyped failure outcomeへの分類は
  `AnsweringRunner`のexternal query phaseが所有する。transport timeoutと同じ定数へ統合しない。
- query 数上限、task 並列数、partial failure の扱い、candidate pool の組み立ては
  回答 workflow policy として `AnsweringRunner` 側の明示的な工程に置く。
- DeepSeek の structured-output 用 function schema は model output schema であり、
  実行可能な Tool registry に含めない。

#### Context、error、resource、observability

- `QuestionContext` は1回の回答 Run で最大1回だけ準備し、同じinstanceを必要な後続phaseへ渡す。
- `previous_answer` はbounded history内の最新assistant本文を加工せず使い、該当がなければ空文字列とする。
- hookは`original_question`、`has_history`、同じ`QuestionContext`だけを1回受け取り、raw historyや
  `previous_answer`を受け取らない。context preparation / hook例外時は全後続phase / Tool callを短絡する。
- context preparation と `on_answering_context_prepared` は、不要な provider / Tool resource を
  開く前に完了する。
- direct pathでは`ExternalResearchRuntimeFactory`をactivateせず、外部検索用Agent / Tool clientを
  作成・openしない。
- internal-only pathでも同factoryをactivateせず、External Search Toolと外部検索用Agentを起動しない。
- Agent / Tool runtime が取得した resource は、成功・typed failure・想定外例外・cancel の全経路で
  解放する。
- PR1ではPlanner用Gemini async clientをcomposition所有のscope factoryからplanning phaseごとに取得し、
  最大2 attemptで共有してphase終了時にcloseを1回試行する。workerや`AnsweringRunner.run()`全体では
  scopeを開かず、Question Context、internal embedding、Direct / Evidence Answerの既存clientへ共有しない。
- 最終形では、共通Runtimeへ移行済みのGemini consumerについて、同じ`AnsweringRunner.run()` invocationで
  最初に必要になった時点からfinal outputまで同じrun-local client poolを再利用可能にする。生成・破棄は
  composition、activation timingはworkflow owner、1 attemptの利用はRuntimeが所有し、別の回答Runへ
  clientを再利用しない。
- Geminiのrun-local共有はDeepSeek / Tavily等のexternal resource scopeと同一視しない。external resourceは
  external branchでだけactivateする既存契約を維持する。
- 明示的なconnection pool上限やkeep-alive tuningは現要件に含めず、provider clientのSDK既定値を使う。
  pool tuningが必要になった場合も、Agent / Runtime / Serviceではなくcompositionのclient factoryで行う。
- provider、retry、fallback、validation、metrics、progress、delta、continuationの既存意味と順序は、
  明示的に変更する別仕様がない限り維持する。PR1では旧audit code / prompt version値との連続性を
  要求せず、新しいAgent / Runtime契約に合わせてaudit記録側も更新してよい。
- 既存policyがtask / query単位のpartial failureへ変換するtyped errorはその場で分類する。それ以外の
  現在workerへ貫通するtyped errorをRunner独自のgeneric error codeへ潰さず、既存分類境界まで伝播する。
- `AnswerGenerationStopped` は同じ exception instance のまま worker へ伝播し、
  `agent_answering_run` を error 扱いしない。
- `agent_answering_run` の明示 attribute は `run_id` だけとする既存制約を維持する。
- Agent phase / attemptとTool call spanのattribute、event、status descriptionにquestion、history、prompt、
  query text、candidate snippet、evidence本文、previous answer、final outputを載せない。
- PR1で導入する初期Agent契約ではPrompt versionを固定Prompt宣言の手動revisionとして`AgentPrompt`が
  関連づけ、記録したいconsumerが`agent.prompt.version`から直接参照する。旧call signature、hash値、
  adapter属性探索を新境界へ持ち込まず、
  Runtime戻り値やprovider requestにも含めない。

### Non-goals

- `openai-agents` dependency を追加すること。
- OpenAI Agents SDK の class や signature を互換 API として再実装すること。
- model-driven tool call loop、handoff、agent-as-tool、approval / resume を導入すること。
- workflow 全体を1つの Agent promptへ置き換えること。
- nested Runner、child `AgentRun`、外部検索単位の conversation / session persistence を追加すること。
- generic workflow DSL、graph engine、step registryを作ること。
- 固定instructionsやtask inputの文面自体を、責任分離と無関係に書き換えること。
- Prompt versionを旧call signature / content hashから自動算出すること。
- 旧prompt version値、旧audit code、既存audit recordとの連続性を維持すること。
- Agent経路へprovider rate limit gateを新規適用すること。
- clean splitに必要な条件文・配置以外のprompt semantics、model、temperature、retry回数、provider、
  検索上限を変更すること。
- provider credentialの名前・設定元・開始APIでのfail-fast検証時点を変更すること。
- API response、DB schema、Alembic migration、Redis event schema、Taskiq message schema、
  authentication / authorizationを変更すること。
- persistent completion、failure persistence、terminal通知を `AnsweringRunner` へ移すこと。
- frontend表示や公開APIへ Agent / Tool 名を露出すること。

### Done

- `Run` がユーザー入力から最終 `RunResult` までの1回だけを意味すると、仕様・型・名前で
  一貫して読み取れる。
- `AnsweringRunner.run()` から question context preparation、planning、retrieval分岐、
  query生成、検索、evidence選別、answer生成、`RunResult`構築の順序を追える。
- workflow全体を表す `QuestionAnsweringAgent` / `QuestionAnsweringOrchestrator` が不要になり、
  Agentは個別のLLM役割として宣言される。
- `ExternalSearchResearchRunner` が所有していた query Agent、Search Tool、selector Agent、
  workflow policyが分離され、部分処理に `Runner` / `RunResult` 語を使わない。
- direct / internal / external / mixed の既存出力とfailure semanticsが維持される。
- Agent phase / attemptとTool callが同じ `agent_answering_run` traceのchild spanとして観測できる。
- Agent phase / attemptとTool call spanのattribute、event、status descriptionにmodel-visible textや検索本文が
  含まれない。
- external resourceはexternal branchだけでactivateし、direct / internal-onlyでは生成しない一方、
  開始APIのcredential fail-fast契約は維持される。
- workerのDB / Redis / Taskiq責任と、API / DB / dependency contractに差分がない。
- 後続実装をレビュー可能なsliceへ分けられ、各sliceのテスト条件が本仕様から導ける。

## 用語と責任境界

### `AgentRun`

Postgresに永続化される、1つのuser turnの状態機械。`queued | running | completed | failed`、
attempt epoch、final answerとの関連を持つ。部分検索やAgent phase / attemptごとには作らない。

### `AnsweringRunner`

1つのユーザー入力から最終 `RunResult` までのin-process回答workflowを進行するapplication component。
context preparation、Agent phase / Tool callの順序、Pythonによる分岐、外側trace、最終結果構築を
所有する。

現在の `Runner` は回答専用の `RunInput`、`RunContext`、`AnsweringRunContext`、
`agent_answering_run` を使っているため、移行時に `AnsweringRunner` へ改名する。

`AnsweringRunContext.run_context` は回答Runの識別子と基準時刻を保持する値であり、
「run fieldへ別contextを入れる」という意味ではない。質問の意味的整理結果は
`question_context`、bounded historyから抽出した直前回答は`previous_answer`として分ける。

### Agent phase / attempt

Agent phaseは、`AnsweringRunner`が所有する1つのLLM役割の処理単位である。role固有のretry、
safe fallback、draft finalizationを適用し、0回以上のAgent attemptを進行する。Agent phaseはRunではなく、
永続状態やconversationを新規作成しない。

Agent attemptは、1つの`Agent`宣言と型付きinputを`AgentRuntime.invoke()`へ渡し、1回のprovider
requestからschema parse済みdraftを受け取る単位である。streaming responseも開始からcloseまでを
1 attemptと数える。

### `Agent`

1つのLLM役割に必要なstatic declaration。stable name、`AgentPrompt`、model target / settings、output type /
response schemaを関連づける。provider clientやrun-local stateを保持しない。

`AgentPrompt[InputT]`と`Agent[InputT, OutputT]`は`@dataclass(frozen=True, slots=True)`として定義し、
各roleは共通classのinstanceとして宣言する。Protocolとrole固有subclassへの分離は、異なるAgent表現が
実際に必要になるまで導入しない。

PR1で導入する初期Agent契約の`AgentPrompt`は手動revisionである`version`、宣言時に確定する
`instructions: str`、
`input_renderer: Callable[[InputT], str]`を持つ。実行ごとに変わるtask inputはinput rendererが同期的かつ
決定的にmodel-visibleな文字列へ変換する。
rendererは`InputT`以外へ暗黙に依存せず、DB、外部API、provider clientへアクセスしない。
必要な外部状態はRunner、role phaseまたはcomposition rootで先に解決して`InputT`へ含める。
固定の役割・判断基準・出力ルールはinstructionsだけに置き、rendererはそれらを複製しない。
provider-backed runtimeは両者をprovider固有の別message fieldへ写像する。
Prompt versionの定数、instructions、固定input templateはrole固有`prompts.py`の同じ編集箇所に置き、
Agent組み立てmoduleは定数を参照するだけとする。rate limit policyはAgent fieldに含めない。
`response_schema`のmodel-visibleな構造、enum、required、descriptionを変更するときも、同じ編集でversionを
更新する。`output_type`だけの変更、model、model settings、実行時inputの変更ではversionを更新しない。

少なくとも次を表現できなければならない。

| 項目 | 意味 |
|---|---|
| `name` | traceとdiagnosticで使うlow-cardinalityな役割名 |
| `prompt.version` | 固定Prompt宣言を識別する、本文moduleで明示した手動revision |
| `prompt.instructions` | 宣言時に確定する、そのAgentだけが行う判断・生成の固定指示 |
| `prompt.input_renderer` | 型付き実行inputだけからmodel-visibleなtask inputを作る同期純粋関数 |
| `model` | provider / modelを識別する`ModelTarget` |
| `model_settings` | provider-neutralな生成調整値を持つ`ModelSettings` |
| `InputT` | Runnerから投影される入力を表すgeneric type parameter。instance fieldではない |
| `output_type` | validation済みの役割固有出力 |
| `response_schema` | modelへ期待するwire outputと行動上のdescriptionを伝えるschema |

`output_type`はPython側parse契約、`response_schema`はmodel向けwire契約であり、一方から他方を
自動生成しない。両者の整合性はcontract testで守る。

### `AgentRuntime`

`Agent`宣言と型付きinputを1回のprovider callへ変換する共通実行境界。Runnerはrole phaseから
`AgentRuntime.invoke(agent, input, attempt_number=n)`を必要回数だけ呼ぶ。`attempt_number`はphase policy
ownerが正の整数として明示し、runtimeはretry stateを保持・推測しない。runtimeはattempt単位のprovider error
translation、schema parse、stream cleanup、attempt spanを所有し、成功時は検証済み`OutputT`だけを返す。
provider responseにusageがあれば、blocked / parse / validation判定より前にattempt spanへ記録する。
provider callがresponseを返さず分類済みprovider errorになった場合はusageを捏造せず、
`result="provider_error"`、error、span durationを記録する。未分類例外では`result`を捏造しない。
usageをService、Runner、`RunResult`の戻り値へ運ばない。

role固有の複数attempt retry、safe fallback、draft finalizationは`AgentRuntime`へ隠さず、
`AnsweringRunner`のrole phase / policyとして既存契約を維持する。`AgentRuntime`は他のAgentやToolを
起動しない。compositionは`Agent`とは別にprovider-backed `AgentRuntime`を生成するscope factoryを
現在のphase ownerへ注入し、workflow ownership移行後はRunnerがactivation timingを所有する。

provider-neutral portは`backend/app/agent/runtime/contract.py`の`AgentRuntime`、Gemini-backed実装は
`backend/app/agent/runtime/gemini.py`の`GeminiAgentRuntime`とする。`GeminiAgentRuntime` instanceが保持する
実行依存は借りたGemini async clientだけで、Agent宣言、task input、retry state、usage accumulator、
Runner / Service参照を保持せず、`invoke()`内でclientをcloseしない。provider-neutralな
`AgentRuntimeScopeFactory`もruntime contractに置き、その実装だけをcompositionが所有する。
`agent/runtime/__init__.py`はprovider-neutralなcontractだけを再公開し、Gemini具象を暗黙にloadしない。
compositionは具象moduleから明示importする。Gemini Runtimeへproviderが`gemini`でないAgentを渡した場合は、
renderer、config、span、provider requestより前に誤配線として拒否する。
PR2のexternal DeepSeek移行だけは、既存clientにclose ownerがないresource debtをPR4まで維持するため、
`AgentRuntimeScopeFactory`を先行導入せず、compositionが既存deferred回答graph構築内で作ったQuery用・
Selector用Runtime instanceを一時ownerへ直接注入する移行例外とする。PR4でbranch-scoped
`ExternalResearchRuntimeFactory`へ置き換え、この例外を削除する。
共通`AgentPrompt` / `Agent` / `ModelTarget` / `ModelSettings`は
`backend/app/agent/agent.py`、role固有のPrompt本文とAgent宣言は各feature packageへ置く。
Planner Prompt本文は`backend/app/agent/planning/prompts.py`、Planner宣言は
`backend/app/agent/planning/agent.py`へ置く。

### Tool

Runnerが明示的に呼ぶ実行能力。ToolはLLM人格やinstructionsを持たず、型付きinputから
型付きoutputを返す。外部I/Oを行うToolはprovider adapterとresource lifecycleを内包できるが、
workflow分岐やmodel判断は所有しない。

Runnerが決定的に呼ぶ初期設計で必要なTool contractは、stable name、typed input / output、
invoke port、failure contractである。modelへ公開するdescriptionやJSON schema registryは、
model-driven tool selectionを採用しない限り追加しない。

internal retrievalもRunnerから呼ぶcapabilityではあるが、本仕様だけではTool宣言へ移行することを
確定しない。現在のrepository / embedding / cache境界を調査した後、External Search Toolと同じ
contractにすることでconsumerが単純になる場合だけ後続sliceで決める。

### Workflow

本仕様におけるworkflowは独立したAgentやgeneric graph objectではない。
`AnsweringRunner.run()` と、その責任を読みやすく分割したprivate methodに表現される
Agent / Toolの実行順と分岐を指す。

### Worker

Taskiq process上のpersistent run lifecycle owner。Runnerの前後でattempt取得、履歴query、
完了・失敗永続化、terminal通知を行う。LLM役割の順序は所有しない。

### Composition

Agent、Agent runtime、Tool、repository、reporterを `AnsweringRunner` へ配線する場所。
workflow順序やdomain分岐を実装しない。

### `ExternalResearchRuntimeFactory`

compositionが実装して`AnsweringRunner`へ注入するasync context-manager factory port。
`activate()`は1回答Runのexternal branch専用に、External Search Query / Selector Agentが使う
`AgentRuntime` bindingとExternal Search Toolをまとめた`ExternalResearchRuntime`を返す。

`AnsweringRunner`がexternal / mixedのexternal枝でだけscopeを開始し、factoryがclient open後の
途中構築失敗を含む全経路でcloseする。同じscope内では安全なclientを共有できるが、別の
`AnsweringRunner.run()` invocationへresourceを再利用しない。direct / internal-only pathでは
Query / Selector Agent clientもTavily clientも生成しない。

移行途中のPR4〜PR7では、まだretrieval分岐を所有する既存external capability adapterが同じ
`activate()` scopeを一時的に開始する。PR8でretrieval dispatchを`AnsweringRunner`へ移すときに、
factory activationのownerも同時に移し、adapter側の一時seamを削除する。

## 現在の実行構造

```text
run_agent_answer                                  # worker
├─ persistent attempt取得
├─ reporter / continuation構築
├─ bounded history query
├─ build Runner / deferred starting agent
└─ Runner.run
   ├─ QuestionContextService.prepare
   ├─ hook
   └─ starting_agent.answer                       # 内部順序が見えない
      ├─ safe HTTP client open
      ├─ concrete graph build
      └─ QuestionAnsweringOrchestrator.answer
         ├─ planner
         ├─ Python match
         ├─ direct answer、または
         └─ EvidenceCollectionService
            ├─ internal search
            └─ ExternalSearchResearchRunner
               ├─ DeepSeek query generator
               ├─ Tavily search provider
               └─ DeepSeek evidence selector
```

問題は処理がtrace外にあることではない。`starting_agent.answer()` は
`agent_answering_run` spanの内側なので、内部instrumentationは同じtraceに属する。
問題は、Runnerの責任としてAgent / Toolの順序がコード上に現れず、Agent・Tool・workflowの
設定と実行境界が別名のobjectへ分散していることである。

## 目標責任モデル

| 責任 | Worker | AnsweringRunner | Agent | Tool | Composition |
|---|:---:|:---:|:---:|:---:|:---:|
| persistent attempt取得 / 冪等性 | ○ | - | - | - | - |
| bounded history DB query | ○ | - | - | - | - |
| question context preparation順序 | - | ○ | 実行 | - | 配線 |
| planning | - | 起動 | 実行 | - | 配線 |
| direct / retrieval分岐 | - | ○ | - | - | - |
| exact external query生成 | - | 起動 | 実行 | - | 配線 |
| external HTTP検索 | - | 起動 | - | 実行 | 配線 |
| evidence選別 | - | 起動 | 実行 | - | 配線 |
| internal / external / mixed並列policy | - | ○ | - | capability実行 | - |
| answer draft生成 | - | 起動 | 実行 | - | 配線 |
| citation / final status assembly | - | ○ | - | - | - |
| progress stageの順序と発火 | - | ○ | - | - | reporter配線 |
| outer run span | - | ○ | child span | child span | - |
| external runtime scope開始・終了 | - | external枝でだけ○ | 利用 | 利用 | factory実装 |
| external client生成・途中失敗cleanup | - | - | - | - | factory |
| DB completion / failure persistence | ○ | - | - | - | - |
| terminal通知 | ○ | - | - | - | - |

## 目標AgentとTool

### Agent roles

| Agent role | 入力 | 出力 | 所有しないもの |
|---|---|---|---|
| Question Context Agent | raw question、bounded history、`as_of` | context draft | fallback決定、run永続化 |
| Question Planner Agent | 完成済み`QuestionContext`、`as_of` | `QuestionPlanDraft` | retry / fallback / plan確定、検索実行、回答生成 |
| External Search Query Agent | `ExternalResearchTask`、time window、`as_of` | validation済みquery list | HTTP検索、candidate選別 |
| External Evidence Selector Agent | sanitized task、URLなしcandidate projection、`as_of` | index参照の`EvidenceSelectionResult` draft | HTTP検索、source metadata決定、final answer |
| Direct Answer Agent | answering input、previous answer | direct answer draft | plan分岐、検索 |
| Evidence Answer Agent | answering input、normalized evidence | evidence answer draft | evidence収集、final status assembly |

Question contextのsafe fallback、planner / answererのretry・finalization、citation backstopなど、
既存service / flowが持つ決定的policyをAgent promptやgeneric Agent runtimeへ移さない。
`AnsweringRunner` が呼ぶrole phase / policyに残し、その具体型だけを各Agent contract sliceで決める。

Planner Agentのoutputは完成済み`QuestionPlan`ではない。Runnerが進行するplanning phaseが
既存`QuestionPlanningService`相当のretry、draft validation、safe fallback、finalizationを適用し、
初めて`NoRetrievalPlan | InternalRetrievalPlan | ExternalSearchPlan | InternalAndExternalPlan`を返す。

### Tool roles

| Tool | 入力 | 出力 | 所有しないもの |
|---|---|---|---|
| Internal Retrieval Capability（暫定名） | validation済みinternal query群 | article search hit | plan判断、answer生成 |
| External Search Tool | 完成済みquery、limit | external search candidate | query生成、evidence選別 |

Embedding生成はinternal search capabilityの実装詳細として扱い、生成LLM Agentと同じ語彙にしない。
将来embeddingを独立Toolにするのは、複数consumerが同じcontractを必要とした場合だけ検討する。

## 目標実行順

### 共通前処理

```text
AnsweringRunner.run(input, run_context, hooks)
├─ agent_answering_run span開始
├─ question context preparation
│  ├─ 必要なら Question Context Agent phase
│  └─ deterministic finalize / safe fallback
├─ AnsweringRunContext構築
├─ on_answering_context_prepared
├─ progress: planning
├─ planning phase
│  ├─ Question Planner Agent phase
│  └─ retry / validation / safe fallback / finalization
└─ typed planをPython match
```

context preparationまたはhookが失敗した場合、planner、検索Tool、answer Agentを起動しない。

### Direct path

```text
NoRetrievalPlan
├─ progress: synthesizing
└─ Direct Answer Agent
   └─ RunnerがAnswerQuestionResult / RunResultを構築
```

External Search Tool、Internal Retrieval Capability、Evidence Answer Agentは起動しない。

### Internal path

```text
InternalRetrievalPlan
├─ progress: retrieving
├─ Internal Retrieval Capability
├─ evidence normalization
├─ progress: synthesizing
└─ Evidence Answer Agent
   └─ Runnerがcitation、sources、missing、statusをassembly
```

External Search Query Agent、External Search Tool、External Evidence Selector Agentは起動しない。

### External path

```text
ExternalSearchPlan
├─ progress: retrieving
├─ taskごとに既存上限でbounded concurrency
│  ├─ External Search Query Agent
│  ├─ queryごとにExternal Search Tool
│  ├─ candidate pool構築
│  └─ External Evidence Selector Agent
├─ task report / external outcome構築
├─ evidence normalization
├─ progress: synthesizing
└─ Evidence Answer Agent
   └─ Runnerがcitation、sources、missing、statusをassembly
```

Query Agentが返したqueryは、既存のstrip、文字数cap、同一文字列の重複排除、件数capだけを
決定的に適用してToolへ渡す。Toolはqueryを生成・拡張・言い換えしない。

### Mixed path

```text
InternalAndExternalPlan
├─ progress: retrieving
├─ Internal Retrieval Capability              ─┐
└─ external pathのtask/query/selection工程      ─┴─ 固定2枝を並行実行
   ├─ partial failureを既存policyで集約
   ├─ evidence normalization
   ├─ progress: synthesizing
   └─ Evidence Answer Agent
      └─ Runnerがcitation、sources、missing、statusをassembly
```

internal / externalの2枝に新しいconcurrency limitは設けない。external枝の内側だけで既存task上限を
適用する。どちらかが分類済み失敗となっても、現在のpartial result policyを維持する。

## AnsweringRunnerの目標形

次は責任を示す概念例であり、最終signatureではない。

```python
class AnsweringRunner:
    async def run(
        self,
        input: RunInput,
        *,
        run_context: RunContext,
        hooks: RunHooks | None = None,
    ) -> RunResult:
        with answering_run_span(run_context):
            answering_context = await self._prepare_answering_context(
                input=input,
                run_context=run_context,
                hooks=hooks,
            )
            plan = await self._plan_question(
                answering_context,
            )

            match plan:
                case NoRetrievalPlan():
                    final_output = await self._answer_directly(
                        answering_context,
                    )
                case InternalRetrievalPlan():
                    final_output = await self._answer_from_internal_evidence(
                        plan,
                        answering_context,
                    )
                case ExternalSearchPlan():
                    final_output = await self._answer_from_external_evidence(
                        plan,
                        answering_context,
                    )
                case InternalAndExternalPlan():
                    final_output = await self._answer_from_mixed_evidence(
                        plan,
                        answering_context,
                    )

            return RunResult(
                final_output=final_output,
                context=answering_context,
            )
```

private methodへの分割は、別Runや別workflow objectを作ることを意味しない。すべて同じ
`AnsweringRunner.run()` invocation、`RunContext`、`agent_answering_run` spanに属する。

## External pathの目標形

external branch全体で1つのresource scopeを開き、task間ではそのscopeを共有する。

```python
async def _collect_external_evidence(self, plan, answering_context):
    async with self._external_runtime_factory.activate() as external:
        return await self._collect_external_tasks(
            plan.external_research_tasks,
            answering_context=answering_context,
            target_time_window=plan.target_time_window,
            external=external,
        )
```

```python
async def _collect_external_task(
    self,
    task_index: int,
    task: ExternalResearchTask,
    *,
    answering_context: AnsweringRunContext,
    target_time_window: str | None,
    external: ExternalResearchRuntime,
) -> ExternalTaskCollectionResult:
    query_draft = await self._generate_external_queries(
        runtime=external.agent_runtime,
        task=task,
        as_of=answering_context.run_context.as_of,
        target_time_window=target_time_window,
    )
    if query_draft is None:
        return self._query_generation_failed(task_index, task)
    queries = self._normalize_external_queries(query_draft.queries)
    if not queries:
        return self._query_generation_failed(task_index, task)

    await self._report_queries_generated(task_index, queries)
    query_outcomes = await self._search_queries(
        queries,
        search_tool=external.search_tool,
    )
    if all(outcome.provider_failed for outcome in query_outcomes):
        return self._provider_failed(task_index, task, queries, query_outcomes)

    candidates = self._build_candidate_pool(query_outcomes)
    await self._report_candidates_fetched(task_index, candidates)
    if not candidates:
        return self._empty_candidate_success(task_index, task, queries, query_outcomes)

    selection_outcome = await self._select_external_evidence(
        runtime=external.agent_runtime,
        task=task,
        candidates=candidates,
        as_of=answering_context.run_context.as_of,
    )
    if selection_outcome.result is None:
        return self._selector_failed(
            task_index,
            task,
            queries,
            query_outcomes,
            candidates,
            failure_reason=selection_outcome.failure_reason,
        )

    result = self._build_external_task_result(
        task_index=task_index,
        task=task,
        queries=queries,
        query_outcomes=query_outcomes,
        candidates=candidates,
        selection=selection_outcome.result,
    )
    await self._report_evidence_selected(task_index, len(result.evidence))
    return result
```

```python
async def _search_queries(
    self,
    queries: tuple[ExternalSearchQuery, ...],
    *,
    search_tool: ExternalSearchTool,
) -> list[ExternalSearchQueryOutcome]:
    return await asyncio.gather(
        *(
            self._search_query(query, search_tool=search_tool)
            for query in queries
        )
    )

async def _search_query(
    self,
    query: ExternalSearchQuery,
    *,
    search_tool: ExternalSearchTool,
) -> ExternalSearchQueryOutcome:
    try:
        candidates = await asyncio.wait_for(
            self._invoke_tool(
                search_tool,
                input=ExternalSearchToolInput(
                    query=query,
                    limit=EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
                ),
            ),
            timeout=PROVIDER_SEARCH_TIMEOUT_SECONDS,
        )
    except (ExternalSearchProviderError, TimeoutError):
        return ExternalSearchQueryOutcome.provider_failure(query)
    return ExternalSearchQueryOutcome.success(
        query,
        candidates[:EXTERNAL_SEARCH_CANDIDATES_PER_QUERY],
    )
```

`_generate_external_queries()` と `_select_external_evidence()` は内部でmodel-visible projectionを
明示的に作る。前者はuntrustedなresearch goalを既存形式で区切り、後者はsanitize済みの
candidate index / title / source name / published at / snippetを含めてURLだけを除外する。
Selector outputからsource identityやsource metadataを採用せず、validation済みindexから元の
Tool candidateを引き直す。claim、why selected、missingは既存validation / cap後のSelector outputを
維持する。

上の`_generate_external_queries()`はExternal Search Query Agent attemptとtimeout / typed failure分類、
`_select_external_evidence()`はExternal Evidence Selector Agent attemptを含む既存2 attempt policyと最後の
failure reasonを表す。前者が`None`を返すのは分類済みfailure / timeoutだけであり、未分類例外は伝播する。
query normalizationはstrip、文字数cap、同一文字列の重複排除、件数capをこの順で適用する。
query単位のtyped provider failureだけをoutcomeへ変換し、未分類例外は握りつぶさない。

実装では既存timeout、retry、partial failure、event順序をテストで固定してから責任を移す。

## Naming migration

同じ名前を異なる概念へ使わないため、移行完了時の語彙を次にそろえる。

| 現在 | 目標 | 理由 |
|---|---|---|
| `Runner` | `AnsweringRunner` | 回答workflow固有のRunnerであることを示す |
| `QuestionAnsweringAgent` | 役割別`Agent`とRunner-owned role phaseへ分解 | workflow全体をAgentと呼ばない |
| `QuestionAnsweringOrchestrator` | `AnsweringRunner`へ責任移動後に削除 | workflow ownerを一つにする |
| `_DeferredQuestionAnsweringAgent` | `ExternalResearchRuntimeFactory`等のbranch factoryへ分解 | resource lifecycleをworkflow Agentに隠さない |
| `ExternalSearchResearchRunner` | 削除してAgent / Tool / Runner policyへ分解 | 部分処理をRunnerと呼ばない |
| `QueryGenerator` | External Search Query Agent / typed contract | LLM役割を明示する |
| `EvidenceSelector` | External Evidence Selector Agent / typed contract | LLM役割を明示する |
| `SearchProvider` | External Search Tool port | 実行可能な検索能力を明示する |
| `TavilySearchProvider` | Tavily External Search Tool adapter | provider実装であることを示す |
| `ExternalSearchRunResult` | `ExternalSearchCollectionResult`等 | 部分処理にRunを使わない |
| `ExternalSearchRequest` | `ExternalSearchCollectionInput`等 | 子Run入力に見せない |
| `ExternalSearchRunner` | External search capability portへ置換 | nested Runnerを作らない |
| `EXTERNAL_SEARCH_AGENT_HARD_LIMIT` | task concurrencyを表す名前 | 並列taskをAgent数と呼ばない |

最終的なclass名はconsumer inventoryと変更理由を確認して各実装sliceで確定するが、
`Run` / `Runner` を部分処理へ残さないことは本仕様で固定する。

## Trace hierarchy

`agent_answering_run`、`agent_phase`、`agent_provider_call`を固定span名とする。実provider requestは
SpanKind `CLIENT`の`agent_provider_call`として表し、API操作、provider、model、usageは標準`gen_ai.*`
attributeへ記録する。Agentのrole、attempt番号等の入力由来値をspan名へ埋め込まない。Tool span名は
対象Tool sliceまたはPR10で確定する。

```text
Taskiq task span
└─ agent_answering_run {run_id}                                # 唯一の回答Run
   ├─ agent_phase {phase=question_context, agent_name}          # LLM補助が必要な場合だけ
   │  └─ agent_provider_call {agent_name, attempt_number, gen_ai.*}
   ├─ agent_phase {phase=question_planning, agent_name}
   │  ├─ agent_provider_call {agent_name, attempt_number=1, gen_ai.*}
   │  └─ agent_provider_call {agent_name, attempt_number=2, gen_ai.*} # retry時だけ
   ├─ agent_phase {phase=direct_answer, agent_name}              # direct時
   │  └─ agent_provider_call ...                                 # attemptごと
   └─ [retrieval branch]                                         # 図上のgroup、spanではない
      ├─ internal_retrieval_call {capability.name}                # internal時
      ├─ agent_phase {phase=external_query, agent_name, task_index} # taskごと
      │  └─ agent_provider_call ...
      ├─ external_search_tool_call {tool.name}                    # queryごと
      ├─ agent_phase {phase=external_selector, agent_name, task_index} # taskごと
      │  ├─ agent_provider_call ...
      │  └─ agent_provider_call ...                              # retry時だけ
      └─ agent_phase {phase=evidence_answer, agent_name}
         └─ agent_provider_call ...                              # attemptごと
```

role phase spanはlogical phase / taskごとに1本、provider attempt spanは実provider requestごとに1本とする。
retryは同じphase配下のattempt spanを増やし、新しいphaseやRunを作らない。attempt usage / latencyは
`AgentRuntime`がattempt spanへ記録し、attempt spanをusage attributeの唯一の正本とする。phase / run spanへ
usageを複写しない。後続sliceで集約表示やmetric consumerを追加する場合もattempt記録から導出し、
戻り値への同乗や別spanへの複写によって同じusageを二重計上しない。

phase、attempt、Tool callはいずれもRun spanではない。図中の`retrieval branch`も実spanを要求しない。
`external_research_run` のような子Run名は追加しない。

`agent_phase`の独自attribute allowlistはlow-cardinalityな`phase`と`agent_name`を基本とし、external Query /
Selectorのtask単位phaseだけはnon-negativeな`task_index`も許可する。`task_index`をprovider attemptへ複写しない。
`agent_provider_call`の独自attribute allowlistは`agent_name`、正の`attempt_number`、分類済み`result`とする。
Logfire / OpenTelemetryが自動付与する内部attributeは、アプリケーション独自attributeのallowlist比較対象外と
する。ロック版Logfire 4.37が`gen_ai.request.model`からexport時に自動補完する`gen_ai.response.model`も、
framework導出のGenAI標準attributeとして同じく比較対象外とする。Runtimeはこの値を明示設定しない。
API操作、provider、model、usageは標準`gen_ai.*` attributeへ記録する。分類済み`result`は
`succeeded | blocked | invalid_response | provider_error`に限定する。usageがprovider responseに存在する
場合だけ、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`、
`gen_ai.usage.cache_read.input_tokens`、`gen_ai.usage.reasoning.output_tokens`へ写像し、欠損を`0`で補わない。
total tokenの独自attributeは作らない。分類済みerrorでは標準`error.type`、descriptionなしのERROR statusを
記録してspanを閉じた後にraiseし、exception eventを作らない。未分類例外は`result`を捏造せずspanから
伝播させ、既存のexception eventを維持する。未分類例外の自由文は既存の`ExceptionRedactingProcessor`が
export境界で`[redacted]`化する。
latencyの正本はattempt span durationとし、重複する`latency_ms`属性を追加しない。

`gen_ai.usage.cache_read.input_tokens`と`gen_ai.usage.reasoning.output_tokens`は現行OpenTelemetry GenAI
registryの綴りを採用する。実装時にlock済みsemantic-conventions packageの定数公開とLogfire panelの
集計表示を照合するが、対応がない場合も独自の代替キーを作らない。

Plannerの`agent_phase`は`phase="question_planning"`、`agent_name="question_planner"`を持つ。
後続roleで追加attributeが必要になった場合は、このallowlistを暗黙に拡張せず、対応sliceでconsumerと
data exposureを再定義する。`run_id`は親`agent_answering_run`だけに置き、childへ複写しない。次は
attribute、event、status descriptionのいずれにも含めない。

- raw question、history、previous answer、final answer。
- instructions、render済みprompt、requirements、previous error、provider response、draft、prompt version。
- query text、URL、title、source name、published at、snippet、claim、why selected、missing本文。
- `QuestionContext`、plan、evidenceのserialize結果。

既存のPII / model-visible text非露出testを親spanからchild spanまで拡張し、attributeだけでなくeventと
status descriptionを含むspan全面を検査する。

## Dependency direction

```text
queue/tasks/agent_run.py
├─ agent/runs                         # persistent lifecycle
├─ agent/threads                      # bounded history
├─ agent/live_updates                 # reporter / stream
├─ agent/composition                  # concrete factories
└─ agent/running.AnsweringRunner      # in-process workflow owner
   ├─ Agent declarations / runtime    # LLM roles / provider attempt
   ├─ tool ports                      # search capabilities
   ├─ question context contracts
   ├─ planning contracts
   └─ answering / evidence contracts

Agent declarations / AgentRuntime do not depend on
  - agent/running.AnsweringRunner
  - agent/runs repository
  - Taskiq / Redis / HTTP endpoint

tool ports do not depend on
  - Agent declarations
  - AnsweringRunner implementation
  - persistent AgentRun repository
```

Runnerはdomain contractへ依存できるが、Gemini、DeepSeek、Tavily、httpx、SQLAlchemyの
具象型へ依存しない。compositionが具象runtime / ToolをRunnerのportへ配線する。

## Failure semantics

移行前後で次を維持する。

- Query Agentの分類済み失敗またはtimeoutは、そのtaskを`query_generation_failed`とし、
  そのtaskのSearch Tool / selector Agentを起動しない。
- queryがvalidation後に空なら同じく`query_generation_failed`とする。
- 一部queryのSearch Tool失敗は他queryを継続し、全query失敗時だけtaskを`provider_failed`とする。
- candidateが空の場合、selector Agentを起動せず、既存の成功・evidence 0件 semanticsを維持する。
- selector Agentの分類済み失敗・timeout・retry回数・failure reasonを維持する。
- task単位failureは他taskを停止せず、既存のtask reportへ反映する。
- mixed retrievalではinternal / externalの分類済みfailureを独立して集約する。
- mixed retrievalのinternal / external固定2枝は`gather(return_exceptions=True)`相当で両側の完了を待つ。
  一方の未分類例外が先に発生しても、他方のcache、metrics、audit等の完了済みside effectを失わない。
- mixed retrievalの未分類例外は両側完了後に伝播し、両側とも未分類例外なら既存どおりinternal側を
  優先する。internalが成功しexternalだけが未分類例外ならexternal例外を伝播する。
- 未分類例外をblank resultへ変換せず、既存worker境界まで伝播する。
- answer Agentのtyped validation error、provider error、continuation停止を既存どおり扱う。
- Agentの分類済みinvalid responseは、provider-neutralな`AgentResponseInvalidError`へ統一する。defectは
  `response_not_json`、`response_not_object`、`output_schema_mismatch`の3値とし、provider名やrole名を
  codeへ焼き込まない。repair hintはfield path、error type、安全と確認した制約値に限定し、生のPydantic
  input、model output、自由文message、URL、任意のvalidation contextを含めない。field pathの文字列は
  output typeが宣言したfield / validation aliasだけを許可し、extra fieldやdict key等の未知componentは
  固定placeholderへ畳む。
- Plannerは上記3 defectだけをrequest内で最大1回retryし、auditのcode / failure reasonへdefect値、
  failure kindへ`ai_response_invalid`を記録する。repair hintやmodel outputをauditへ記録しない。

責任の移動はerror semanticsの変更理由にならない。変更が必要な場合は別specで明示する。

## Resource lifecycle

- PR1のPlanner用Gemini async clientは`QuestionPlanningService.plan()`のphase scopeでactivateし、
  attempt 1 / 2では同じclientを共有する。正常・分類済みfailure・想定外例外・cancel・client取得後の
  Runtime構築失敗でscopeを退出し、compositionのfactoryがclientのcloseを1回試行する。
- PR1ではGemini clientを回答Run全体へ引き上げない。共通Runtimeへ移行していないQuestion Context、
  internal embedding、Direct / Evidence Answerを同時に変更せず、workerもprovider resourceを知らない。
- PR1のclose契約は、SDK async context managerが正常にcloseできる場合の1回の試行までとする。close自体の
  failureに対するretry、元の例外との優先順位・合成、二重cancelやprocess強制終了への耐性は扱わない。
  close failureでは独自にcatch / suppress / combineせず、close例外が本体例外を置き換え得て、元の例外が
  `__context__`に残るPython / SDK既定挙動をPR1の受容済みリスクとする。
- context preparationとhookの完了前にexternal search用resourceを開かない。
- plan確定後、実際にexternal pathを実行するときだけ`ExternalResearchRuntimeFactory.activate()`を
  `async with`で開始する。
- factory scopeはQuery / Selector Agent用runtime bindingとExternal Search Toolをまとめて生成する。
  同一external branch内で共有して安全なclientはtask間で共有できるが、Runnerは具象client型を知らない。
- factoryはclient open後のruntime / Tool構築失敗、正常終了、Agent failure、Tool failure、
  unexpected exception、cancelのすべてでcloseする。
- 同じ`AnsweringRunner` instanceを複数回呼んでも、external resourceは回答Runごとにfreshに生成し、
  invocation間で再利用しない。
- Agent runtimeがstream objectを返す場合、既存の`aclose()`保証を維持する。
- 回答Run全体でのGemini client共有はPR1直後のPR2へ入れず、`AnsweringRunner`のworkflow ownershipと
  Gemini consumerの共通Runtime移行が揃った後続専用sliceで行う。そのsliceではGemini利用箇所をinventoryし、
  最初の必要時にlazy activateしてfinal output後にcloseし、回答Run間では共有しない。
- connection pool数の独自上限は追加せずSDK既定値を使う。worker concurrencyは同時Run数の上流上限であり、
  HTTP connection数そのものの厳密な上限とは扱わない。
- direct / internal-only pathでQuery / Selector Agent clientとTavily HTTP clientをactivateしないことは、
  本移行で意図するresource
  lifecycle改善である。一方、開始APIの既存fail-fastは維持し、DeepSeek / Tavily credentialが
  欠けていればrun作成前に同じ503を返す。credential検証の遅延化は別仕様なしに行わない。

## Hooks、events、metrics

- `on_answering_context_prepared` はcontext完成後、planner Agentより前に1回だけ呼ぶ。
- `planning` progressはhook完了後かつplanning phaseの最初のAgent attempt前、`retrieving`は
  最初のretrieval capability前、`synthesizing`はDirect / Evidence Answer Agent phase前に
  それぞれ1回発火する。direct pathでは`retrieving`を発火しない。
- external query generated、candidate fetched、evidence selected eventは、それぞれ対応する
  Agent / Tool工程が成功した後にだけ発火する。
- raw historyをhook、event、metricへ追加しない。
- 既存`ExternalSearchQueriesGeneratedEvent`と`ResearchTaskReport.generated_queries`は、live/domain
  payloadとして維持できる。ただしquery textをtrace、log、metric label、audit attributeへ転用しない。
- Agent / Tool名は固定定義から取得し、user inputをspan名・metric labelへ埋め込まない。
- Agent attemptとTool callのusage / latency metricは、consumerとcardinality policyを定義する
  後続observability sliceで、attempt / call記録から導出する。phase / run spanへusage attributeを複写しない。
- PR1で導入する初期Agent契約ではPrompt versionを各roleの固定本文moduleで手動revisionとして宣言し、
  必要なaudit consumerが`agent.prompt.version`から直接参照する。旧call signatureの算出・伝播経路は
  移行しない。

## Migration slices

上記InvariantsとTest matrixは移行完了時の最終gateである。中間sliceは、以下で明示した既存の
互換seamだけを一時的に保持できるが、新しいnested Runner、`RunContext`、persistent `AgentRun`を
追加しない。各PRは変更した境界のcontract testと既存regression testを通し、互換seamの削除予定を
次sliceへ明記する。

本仕様は全体の責任、順序、最終gateを定めるumbrella specとする。PR1以降は、着手前にそのPRだけの
Problem / Evidence / Invariants / Non-goals / Doneを定めたslice仕様を別ファイルで作成し、レビュー後に
実装する。PR0は挙動を変えない限定的な改名であり、以下の契約を実装仕様として十分とする。

### PR0: AnsweringRunner naming boundary

#### Problem

回答Runの外側境界がgenericな`Runner`という名前のため、部分処理のRunnerと区別しにくく、今後
workflow責任を移す対象も読み取れない。最初にこの境界を`AnsweringRunner`として固定する。

#### Evidence

- productionの改名対象は`backend/app/agent/running/runner.py`、同package export、composition、
  worker taskの4箇所に閉じている。
- 対応するunit / contract / composition / worker testが、context、hook、starting agent、span、
  error、resource lifecycleの既存挙動を保護している。
- `Runner`はcompositionで構築され、`build_runner()`はworkerから呼ばれる内部factoryであり、
  公開API、DB、serialized payload、plugin contractとして露出していない。

#### Rename contract

- `backend/app/agent/running/runner.py`を`answering_runner.py`へ改名する。
- `Runner`を`AnsweringRunner`へ、`build_runner()`を`build_answering_runner()`へ改名する。
- package export、composition、worker import / local variable、対象test、fake、monkeypatch先を同時に改名する。
- `test_runner.py`を`test_answering_runner.py`へ改名する。
- `Runner = AnsweringRunner`や旧`build_runner()`の互換aliasは残さない。

#### Invariants

- `AnsweringRunner.run()`の引数、戻り値、呼び出し順、呼び出し回数を変更しない。
- context preparation -> hook -> `starting_agent.answer()` -> `RunResult`の順序と短絡を維持する。
- `agent_answering_run` span名、属性、成功・失敗記録を変更しない。
- context / hook / starting agentの例外伝播、composition fallback、workerの永続化・通知・resource解放を
  変更しない。
- workerが回答境界を1回だけ呼ぶ契約と、同じ`AnsweringRunContext`を`RunResult`へ保持する契約を維持する。

#### Non-goals

- workflow責任を`QuestionAnsweringOrchestrator`から移さない。
- `Agent` / `AgentRuntime`を導入せず、`starting_agent`を削除しない。
- `RunInput`、`RunContext`、`RunResult`、`RunHooks`を改名しない。
- `ExternalSearchResearchRunner`や外部検索の部分Run語を改名しない。
- prompt、model、Tool、resource activation、API、DB、dependencyを変更しない。

#### Done

- productionと対象testが新しいmodule / symbol / factory名だけを参照し、旧名の互換aliasが存在しない。
- `RUN-01`、`CTX-01`〜`CTX-04`、`RUN-07`、`OBS-01`、`OBS-04`、`OBS-05`、`OBS-09`、
  `ARCH-01`と既存worker / hook / composition regressionを通す。
- 改名対象外の`ExternalSearchResearchRunner`とそのtest名を変更していない。

残すseam: `QuestionAnsweringOrchestrator`、workflow全体の`QuestionAnsweringAgent`、`starting_agent`。
削除するseam: genericに見える回答境界の`Runner` / `build_runner`名。

### PR1: Agent contract and Planner Agent vertical slice

- 詳細仕様: `backend/specs/planner-agent-runtime-slice.md`
- immutable `Agent`と、1 provider attemptだけを実行するnon-streaming
  `AgentRuntime.invoke(agent, input, attempt_number=n)`を、最初の実consumerであるPlanner Agentと同時に導入する。
- `Agent` / model value objectを`agent/agent.py`、provider-neutral portを`agent/runtime/contract.py`、
  clientだけを保持する`GeminiAgentRuntime`を`agent/runtime/gemini.py`、Planner宣言を
  `agent/planning/agent.py`へ置く。
- Question PlannerのPrompt version / instructions / input rendererを`AgentPrompt`、stable name、
  `ModelTarget`、`ModelSettings`、`QuestionPlanDraft`、手書き`response_schema`をAgent宣言へ関連づけ、
  旧call specのmodel call構成・実行責任を解体する。
- Prompt version、固定instructions、固定input templateを`agent/planning/prompts.py`へ隣接して置き、
  `agent/planning/agent.py`はそのversion定数を参照してPromptを組み立てる。
- `response_schema`のmodel-visibleな構造、enum、required、descriptionを変更するときはPrompt versionも
  明示的に更新する。`output_type`だけの変更、model、model settings、実行時inputの変更では更新しない。
- 固定instructionsをGemini `system_instruction`、render済みtask inputを`contents`へ分離する。
- `AgentRuntime.invoke()`は`QuestionPlanDraft`を直接返し、responseにusageがあればattempt spanへ記録する。
  usage用result envelopeとphase / run集約は追加しない。
- 旧prompt call signature / hash機構は移行せず、audit記録側は`agent.prompt.version`を直接参照する。
  旧version値 / audit codeとの連続性は要求せず、rate limit policyをこのsliceで新規適用しない。
- `agent_phase`と`agent_provider_call`の親子関係、SpanKind `CLIENT`、GenAI標準attribute、独自attribute
  allowlist、分類済みerrorのeventなしraise、未分類例外のredaction、typed input / draft output、schema parseを
  Plannerの実処理で固定する。
- composition所有のRuntime scope factoryをplanning phaseで1回activateし、attempt 1 / 2で同じGemini
  async clientを共有する。正常・例外・cancelでscopeを退出してcloseを1回試行し、Runtimeはcloseしない。
- 実装PRでは代表的な初回 / retry attemptの`system_instruction`と`contents`のgoldenを人間が一読し、
  固定instructions、task input、repair情報の分離を確認する。
- workerや`AnsweringRunner.run()`全体へGemini client scopeを引き上げず、独自のconnection pool設定も
  追加しない。
- 既存planning serviceのretry、fallback、finalization、audit、metricsをgeneric runtimeへ移さない。
- consumerを持たないgeneric runtimeや、Plannerが使わないstream API / stream cleanupを先行実装しない。
- Exit gate: `FLOW-02`、`FLOW-08`、`AT-07`、`RES-08` / `RES-09`、`OBS-02` / `OBS-05`のattempt部分と、
  planningのretry / fallback / audit発火条件を検証する。旧audit code / Prompt version値の一致は
  要求しない。
- 残すseam: `QuestionAnsweringOrchestrator`、workflow全体の`QuestionAnsweringAgent`、`starting_agent`、
  既存Generator / Selector port。削除するseam: Planner固有の重複したmodel / schema設定。

### PR2: External Query and Selector Agent contracts

- 詳細仕様は
  [`external-query-selector-agent-runtime-slice.md`](./external-query-selector-agent-runtime-slice.md)を正本とする。
- `DeepSeekQueryGenerator`と`DeepSeekEvidenceSelector`を、それぞれ独立した`Agent`宣言とtyped input / draft
  outputへ移し、provider-backedな1 attemptを`DeepSeekAgentRuntime`へ分離する。
- Query normalizationとSelectorのclamp / index検証 / source復元はworkflow policyとして
  `ExternalSearchResearchRunner`に残し、Runtimeはdeclared draftのvalidationまでを所有する。
- DeepSeek function callingはstructured-output transportとしてprovider bindingへ置き、実行可能なToolや
  External Search Tool registryへ混入させない。schemaの正本は`Agent.response_schema`、Python parse契約の
  正本は`Agent.output_type`とする。
- 固定instructionsとtask inputをsystem / user messageへ分離し、手動Prompt versionは固定本文と同じmoduleへ
  置く。旧call signature / hash値との連続性は要求しない。
- Runtimeはworkflow retryを行わず、Queryは1 attempt、Selectorは分類済み失敗またはworkflow timeout時だけ
  最大2 attemptとする。client timeout 20秒、workflow backstop 30秒、lock済みOpenAI SDKの既定transport
  retryを別責任として維持する。
- invalid responseはprovider-neutralな3 defectへ分類し、未分類例外は通常失敗へ変換せず伝播する。
  provider usageはparse / validationより前に`agent_provider_call`へ記録し、戻り値や上位spanへ複写しない。
- 既存`ExternalSearchResearchRunner`を一時ownerとして新Agent contractを呼び、工程順はまだ移さない。
  同Runnerは旧QueryGenerator / EvidenceSelector portを経由せずAgent / Runtimeを直接呼び、taskごとの
  `agent_phase`、attempt loop、timeout、normalization / finalizationを所有する。
- Query / Selector用client / Runtimeは別instanceを維持し、既存deferred回答graph構築内でcompositionが
  生成する。PR2ではdirect / internal pathでのclient object生成とclose owner不在を受け入れ、lazy external
  branch scope、client共有、closeはPR4へ送る。
- Exit gate: `AT-05`〜`AT-07`、`ERR-01`、`ERR-05`と既存query generation / selection regressionを通す。
- 残すseam: `ExternalSearchResearchRunner`と既存SearchProvider port。削除するseam: QueryGenerator /
  EvidenceSelector port、Query / Selector固有の重複したmodel / schema / function設定。

### PR3: External Search Tool

- `TavilySearchProvider`を完成済みqueryだけを実行するExternal Search Tool port / adapterへ移す。
- transport timeoutとworkflow backstop、provider errorとworkflow failure outcomeの責任を分ける。
- `ExternalSearchResearchRunner`を一時ownerとしてToolを呼び、task / query policyはまだ移さない。
- Exit gate: `AT-01`〜`AT-04`、`ERR-02`〜`ERR-04`、`REG-05`、`REG-09`と既存Tavily regressionを通す。
- 残すseam: `ExternalSearchResearchRunner`。削除するseam: 実行能力に対する`SearchProvider`語彙。

### PR4: ExternalResearchRuntimeFactory

- branch-scoped `ExternalResearchRuntimeFactory`を実装し、既存external capabilityをそのscopeで動かす。
- Query / Selector Agent runtime clientとExternal Search Tool clientを1つのexternal branch scopeで生成する。
- direct / internal-only pathでresourceをactivateしないための境界を、workflow owner移動前に固定する。
- PR4では既存`ExternalSearchService`がexternal search開始時に`async with factory.activate()`を一時的に
  所有し、PR8で`AnsweringRunner`へ移す。
- 開始APIのcredential fail-fast、error shape、回答結果は変更しない。
- Exit gate: `RES-01`〜`RES-06`、`ARCH-02` / `ARCH-03`と既存resource regressionを通す。
- 削除するseam: `_DeferredQuestionAnsweringAgent`由来のresource factory。残すseam:
  `ExternalSearchService`の一時的なfactory activation ownerと`ExternalSearchResearchRunner`。

### PR5: Top-level workflow ownership

- planning、direct / retrieval分岐、direct / evidence answer phase、answer assembly、progress発火を
  `AnsweringRunner.run()`から追える形へ移す。
- evidence collectionは一時的なcapability portとして呼び、内部実装をこのPRで展開しない。
- Exit gate: `FLOW-01`〜`FLOW-05`、`FLOW-07` / `FLOW-08`、`RUN-03`〜`RUN-07`、
  `ARCH-02` / `ARCH-03`を通す。
- 削除するseam: `QuestionAnsweringOrchestrator`、workflow全体の`QuestionAnsweringAgent`、`starting_agent`。
  残すseam: `EvidenceCollectionService`、external capability、既存`DirectAnswerer` / `EvidenceAnswerer`
  portと、それを実装する`DirectAnswerFlow` / `EvidenceAnswerFlow`。

### PR6: Question Context Agent

- Question Context Agentのstable name、instructions、model、output schemaをAgent宣言へ移す。
- context preparationの1回性、bounded history、`previous_answer`、hook短絡を変更しない。
- Exit gate: `CTX-01`〜`CTX-04`と既存question context preparation regressionを通す。
- 削除するseam: Question Context固有の重複設定。残すseam: 既存Direct / Evidence Answer実装。

### PR7: Direct and Evidence Answer Agents

- Direct Answer、Evidence Answerのinstructions、model、output schemaをAgent宣言へ移す。
- streaming AgentRuntime contractを実consumerと同時に追加し、safe fallback、retry、stream delta、
  continuation、validation、audit、stream cleanupを維持する。
- Exit gate: `REG-01`〜`REG-03`、`RES-07`と既存answer / delta / continuation regressionを通す。
- 削除するseam: Direct / Evidence Answer固有の重複設定と、PR5で残した既存answer flow adapter。

### PR7以降: Gemini run-local resource scope専用slice

- PR1直後のPR2では実施しない。`AnsweringRunner`がtop-level workflowを所有し、Question Context、Planner、
  Direct / Evidence Answer等のGemini consumerが共通Runtime境界へ移行した後に、独立したslice仕様を作る。
- Gemini利用箇所をinventoryし、1つの回答Runで最初に必要になった時点からfinal outputまで同じrun-local
  async client poolを再利用可能にする。別の回答Runやworker process間では共有しない。
- DeepSeek / Tavilyのexternal branch scopeとは分け、不要なprovider resourceを先行生成しない。
- connection pool上限、keep-alive等は計測された要件が生じるまでSDK既定値を使う。

### PR8: Retrieval dispatch ownership

- internal / external / mixed分岐と固定2枝の並行policyを`AnsweringRunner`へ移す。
- external / mixedのexternal枝で`ExternalResearchRuntimeFactory.activate()`を開始するownerも、
  `ExternalSearchService`から`AnsweringRunner`へ移す。
- mixedの両側完走、未分類例外のinternal優先、partial result、cache / metrics side effectを固定する。
- Exit gate: `FLOW-04`〜`FLOW-06`、`ERR-07`〜`ERR-10`、`REG-01`〜`REG-03`を通す。
- 削除するseam: `EvidenceCollectionService`のdispatch ownerと`ExternalSearchService`のfactory
  activation owner。残すseam: external pipeline adapterとしての`ExternalSearchResearchRunner`。

### PR9: External pipeline ownership

- Query Agent phase -> External Search Tool -> Selector Agent phaseを、同じ回答Run内のprivate phaseへ展開する。
- task / query concurrency、normalization、candidate pool、failure report、provenance、task横断dedupeを移す。
- Exit gate: `RUN-02`、`FLOW-05` / `FLOW-06`、`AT-01`〜`AT-07`、`ERR-01`〜`ERR-06`、
  `REG-04`〜`REG-09`、`RES-05` / `RES-06`を通す。
- 削除するseam: `ExternalSearchResearchRunner`と外部部分処理のnested Runner呼び出し。

### PR10: Naming and observability cleanup

- 部分処理に残る`Run` / `Runner`語をconsumer inventoryに基づいて置換する。
- phase / attempt / Tool span、text非露出、attempt記録から導出するusage観測を完成させる。
- Exit gate: `RUN-02`、`OBS-01`〜`OBS-09`、`ARCH-01`〜`ARCH-03`と全regression matrixを通す。
- 削除するseam: `ExternalSearchRunResult`等の部分Run語と移行adapter。

各PRはbehavior-preservingを基本とし、model、provider、公開contractの変更を混ぜない。
例外として、PR1は既存の役割・判断・出力・repair semanticsとtask dataを保ったままprovider message
fieldを分離し、retry条件文、既存promptの文面順序、message構造を変更する。このためlive model outputの
byte同一性は要求しない。
PR4では`ExternalResearchRuntime`（Query / Selector runtime client + External Search Tool client）のactivationを
external branchまで遅らせるresource lifecycle改善を行う。credentialの開始API検証、error shape、
回答結果は変更しない。

## Test matrix

### Context contract

- `CTX-01`: `QuestionContext`を回答Run内で1回だけ準備し、同じinstanceを必要な後続phaseへ渡す。
- `CTX-02`: `previous_answer`へbounded historyの最新assistant本文を加工せず設定し、なければ空文字列にする。
- `CTX-03`: hookを1回だけ呼び、`original_question`、`has_history`、同じ`QuestionContext`だけを渡す。
  raw historyと`previous_answer`は渡さない。
- `CTX-04`: context preparation / hook例外時、planning、Agent phase、retrieval capability、Tool callを
  すべて0回にする。

### Run boundary

- `RUN-01`: workerがRunnerへ到達したattemptは`AnsweringRunner.run()`をちょうど1回呼ぶ。
  history read / composition等のpre-run failureは0回を許し、同一attemptで2回以上は呼ばない。
- `RUN-02`: 移行完了後、direct / internal / external / mixedの全pathでnested Runnerを呼ばない。
- `RUN-03`: 全Agent phase / attempt、capability、Tool callが同じ親run / traceに属し、新しい
  `RunContext`を作らない。
- `RUN-04`: `as_of`を必要とするQuestion Context、Planner、Query、Selector、Answer phaseだけが、
  同じ基準時刻を型付きinputから観測する。
- `RUN-05`: External Search Toolへ`RunContext`や未使用の`as_of`を渡さない。
- `RUN-06`: child operationごとにpersistent `AgentRun`、conversation、sessionを作らない。
- `RUN-07`: `RunResult.context`が同じ`AnsweringRunContext`を保持する。

### Call order and branching

- `FLOW-01`: context preparation -> hook -> `planning` progress -> planning phaseの順序を守る。
- `FLOW-02`: planning phaseはPlanner Agentのdraftにretry / validation / safe fallback / finalizationを
  適用してからcompleted planをPython `match`へ渡す。
- `FLOW-03`: direct pathは`synthesizing` progress -> Direct Answer Agent phaseの順に呼ぶ。
- `FLOW-04`: internal pathは`retrieving` progress -> Internal Retrieval Capability ->
  `synthesizing` progress -> Evidence Answer Agent phaseの順に呼ぶ。
- `FLOW-05`: external pathは`retrieving` progress後、taskごとにQuery Agent phase -> Search Tool ->
  Selector Agent phaseの順に呼び、全task集約後に`synthesizing` progress -> Evidence Answer Agent phaseを呼ぶ。
- `FLOW-06`: mixed pathはinternal / externalの固定2枝を並行実行し、external task内だけ
  既存concurrency上限を使う。
- `FLOW-07`: planに含まれないAgent phase / capability / Toolを起動しない。
- `FLOW-08`: AgentRuntimeやAgent outputから別Agent / Toolを直接起動しない。

### Agent / Tool separation and prompt safety

- `AT-01`: Query Agent outputへstrip、文字数cap、重複排除、件数capを適用したqueryだけを
  External Search Tool inputにする。
- `AT-02`: External Search ToolはQuery Agentを呼ばず、受け取ったqueryを生成・拡張・意味変更しない。
- `AT-03`: External Search Toolはcandidateを返し、`EvidenceSelectionResult`を返さない。
- `AT-04`: Selector AgentはSearch Toolを呼ばず、与えられたcandidate projectionだけを評価する。
- `AT-05`: Query / Selector inputでuntrusted textのsanitizeと境界を維持する。Selector candidateには
  index / title / source name / published at / snippetを含め、URLを含めない。
- `AT-06`: Selector outputはsourceをcandidate indexだけで参照し、範囲外・重複・cap超過をdropする。
  source metadataとsource refはTool candidate pool / task indexから再構築し、validation済みの
  claim / why selected / missingは維持する。
- `AT-07`: structured-output schemaを実行可能Tool一覧へ混入させない。

### Failure and partial result

- `ERR-01`: Query Agentの分類済みfailure時、そのtaskのSearch Tool / Selector Agentを0回にする。
- `ERR-02`: 一部queryのTool failure時、他queryとSelector Agentを既存条件で継続する。
- `ERR-03`: 全queryのTool failure時、Selector Agentを呼ばず`provider_failed` reportを作る。
- `ERR-04`: candidate 0件時、Selector Agentを呼ばず既存の成功statusを維持する。
- `ERR-05`: Selector Agentの2 attempt、timeout、typed failure reasonを維持する。
- `ERR-06`: task failureが別taskをcancelしない。
- `ERR-07`: mixed retrievalの分類済み片側failureで他側のevidenceを失わない。
- `ERR-08`: mixed retrievalの未分類例外時も両側の完了を待ち、成功側のcache / metrics / auditを維持する。
- `ERR-09`: mixedの両側が未分類例外ならinternal例外を優先し、internal成功時はexternal例外を伝播する。
- `ERR-10`: その他の未分類例外を握りつぶさずworkerまで伝播する。

### Resource lifecycle

- `RES-01`: context preparation / hook failure時、`ExternalResearchRuntimeFactory`をactivateしない。
- `RES-02`: PR4以降、direct / internal-only pathでQuery / Selector Agent clientとTavily clientを作らない。
- `RES-03`: DeepSeek / Tavily credential欠落時、開始APIがrun作成前に既存503を返す。
- `RES-04`: client open後にAgentRuntime / Tool / scope構築が失敗しても、factoryがclientをcloseする。
- `RES-05`: external pathの正常終了、Agent failure、Tool failure、answer failure、unexpected exception、
  cancelの全経路でscope resourceをcloseする。
- `RES-06`: 同じRunnerを複数回呼んでもresourceは回答Runごとにfreshで、external branch内だけ共有する。
- `RES-07`: Agent stream resourceを正常・異常の両経路でcloseする。
- `RES-08`: PR1のPlanner Runtime scopeはplanning phaseにつき1回だけactivateし、最大2 attemptで同じ
  Gemini async clientを使う。closeが正常終了するfake clientを、正常・例外・cancel・Runtime構築失敗で
  1回closeし、Runtime単体はcloseしない。同じServiceで`plan()`を2回呼ぶとscopeを2回activateし、
  planning phase間では異なるRuntime / client identityを使う。
- `RES-09`: PR1ではcontext preparation / hook failure時にPlanner Runtime scopeをactivateせず、
  回答Run全体のGemini client共有と独自connection pool設定を追加しない。

### Output and event regression

- `REG-01`: direct / internal / external / mixedの`AnswerQuestionResult`を既存fixtureと同値に保つ。
- `REG-02`: citation unknown ref、missing aspects、collection failures、status capの既存validationを維持する。
- `REG-03`: source順、evidence cap、query cap、candidate cap、task report順を維持する。
- `REG-04`: effective task concurrencyを
  `min(task count, max(1, requested if requested is not None else task count), hard limit)`で決め、
  task 0件時だけ0とする。
- `REG-05`: query別candidateをround-robinでpool化し、URL重複を先勝ちで除外してtask capを守る。
- `REG-06`: task横断evidenceもURLで先勝ちdeduplicateし、残すevidenceのsource refを書き換えず、
  `deduplicated_evidence_count`を一致させる。
- `REG-07`: task report / evidence count、task index、source ref、dropped selection countの整合を維持する。
- `REG-08`: progress、delta、continuationの順序とpayloadを維持する。並列activityはglobal total orderを
  要求せず、各task内のqueries -> candidates -> selectionという因果順序だけを固定する。
- `REG-09`: query normalization後のTool call数、generated event、provider failure countを既存値に保つ。

### Trace and data exposure

- `OBS-01`: `agent_answering_run`を唯一の回答Run spanとする。
- `OBS-02`: logical role phaseを固定名`agent_phase`でphase / taskごとに1本作り、実provider requestごとに
  同phase配下へ固定名`agent_provider_call`のCLIENT spanを1本作る。retryはattempt spanだけを増やす。
- `OBS-03`: `retrieval` groupや`external_research_run`等のchild Run spanを作らない。
- `OBS-04`: 親spanの明示attributeを`run_id`だけにする。
- `OBS-05`: child spanのattribute、event、status descriptionにquestion、history、instructions、prompt、
  requirements、previous error、provider response、draft、prompt version、query text、URL、title、source name、
  published at、snippet、
  claim、why selected、missing、previous answer、final outputのsentinelを含めない。
- `OBS-06`: `agent_phase`の独自attribute allowlistを`phase` / `agent_name`と、external Query / Selector
  phaseだけの`task_index`、`agent_provider_call`の独自attribute allowlistを`agent_name` / `attempt_number` /
  分類済み`result`とする。`task_index`はattemptへ複写しない。API操作、provider、model、usageは
  標準`gen_ai.*`、errorは標準`error.type`へ記録する。input由来文字列、childへの`run_id`複写、
  独自total token、重複`latency_ms`を含めない。Logfire / OpenTelemetry内部attributeはアプリケーション独自
  attributeのallowlist比較対象外とする。Logfire 4.37が自動補完する`gen_ai.response.model`もframework導出の
  標準attributeとして同じく比較対象外とし、Runtimeは明示設定しない。
- `OBS-07`: generated queriesは既存live event / task reportに残るが、trace、log、metric label、
  audit attributeへ出力しない。
- `OBS-08`: attempt usageはprovider responseを所有するRuntimeがattempt spanへ記録し、usage attributeの
  唯一の正本とする。phase / run spanへ複写しない。集約表示やmetricはattempt記録から導出し、同じusageを
  二重計上しない。
- `OBS-09`: `AnswerGenerationStopped`をerror eventにしない。分類済みAgent response errorは`result`、
  `error.type`、descriptionなしのERROR statusを記録してspanを閉じた後にraiseし、exception eventを作らない。
  未分類の貫通例外は既存exception eventを維持し、export後の自由文を`[redacted]`化する。

### Architecture boundary

- `ARCH-01`: workerはpersistent lifecycle、bounded history、completion / failure persistence、terminal通知だけを
  所有し、semantic workflowは1つの`AnsweringRunner.run()`へ委譲する。
- `ARCH-02`: `AnsweringRunner`は注入されたAgentRuntime、Tool、capability、factory portだけを使い、
  SQLAlchemy、Taskiq、Redis、httpx、Gemini、DeepSeek、Tavily具象をimport・構築しない。
- `ARCH-03`: compositionだけがprovider-backed runtime / Tool / factory具象を配線し、workflow分岐を持たない。

## Expected file areas

実装slice全体では、主に次の領域を変更する。各PRは必要なsubsetだけを扱う。

```text
backend/app/agent/agent.py                         # AgentPrompt / Agent / ModelTarget / ModelSettings
backend/app/agent/runtime/contract.py              # AgentRuntime port
backend/app/agent/runtime/gemini.py                # GeminiAgentRuntime
backend/app/agent/runtime/deepseek.py               # DeepSeekAgentRuntime / output binding contract
backend/app/agent/running/                         # AnsweringRunner contracts / orchestration
backend/app/agent/question_context/                # Question Context Agent migration
backend/app/agent/planning/contract.py             # PlanningAttemptInput / Planner domain contracts
backend/app/agent/planning/prompts.py              # Prompt version / fixed text / input renderer
backend/app/agent/planning/agent.py                # Planner AgentPrompt / Agent declaration
backend/app/agent/planning/                        # Planner phase policy / domain contracts
backend/app/agent/evidence_collection/             # Tool ports / collection policy
backend/app/agent/evidence_collection/external_search/
backend/app/agent/answering/                       # direct/evidence Agent + assembly移行
backend/app/agent/composition.py                   # concrete wiring
backend/app/queue/tasks/agent_run.py               # AnsweringRunner 1回呼び出し
backend/tests/agent/                               # contract / ordering / regression / trace
```

共通Agent宣言とprovider runtimeはPR1で上記配置へ導入し、role固有宣言は対応するfeature packageへ
同居させる。consumerを持たない追加runtime / abstraction directoryは先行して作らない。

## API / DB / dependency

本設計はbackend内部の実行責任を整理するものであり、次を変更しない。

- FastAPI request / response schema。
- OpenAPIとgenerated frontend types。
- SQLAlchemy modelとAlembic migration。
- Postgresの`AgentRun` / thread / message / source semantics。
- Redis stream / recent event schema。
- Taskiq message schemaとtask name。
- 認証・認可。
- Python / frontend dependency。
- provider API keyの設定経路。
- 回答開始APIがDeepSeek / Tavily credentialをrun作成前に検証し、欠落時に503を返すfail-fast契約。

## PR1詳細仕様で確定した実行境界

PR1詳細仕様では、`AgentPrompt` / Agentの責任とfield、Prompt versionの編集局所性、instructions / inputの
分離、Runtimeの直接`OutputT`返却に加え、
次を確定した。

- `AgentRuntime` / `GeminiAgentRuntime`の名前、配置、clientだけを保持するinstance責任。
- 呼び出し側が明示する正の`attempt_number`と、retry stateをRuntimeへ持たせない契約。
- Prompt versionの正本を固定本文moduleへ置き、記録側が`agent.prompt.version`を直接参照する契約。
- 固定名`agent_provider_call`、SpanKind `CLIENT`、GenAI標準attribute、独自attribute allowlist、
  renderer失敗時にspanを作らず、分類済みerrorはeventなし、未分類例外はredaction済みeventとする
  Generation span契約。
- composition所有の`AgentRuntimeScopeFactory`をplanning phaseで1回activateし、最大2 attemptで同じGemini
  async clientを共有した後、全終了経路でcloseを1回試行するclient lifecycle契約。Runtimeはclientを
  closeしない。
- 回答Run全体のGemini client共有はPR1 / PR2へ含めず、共通Runtime移行後の専用sliceへ送ることと、
  connection poolは計測された要件が生じるまでSDK既定値を使う方針。
- model-visible text、入力由来文字列、Prompt version、childへの`run_id`複写を禁止するdata exposure境界。

これらは、Agentがworkflowを所有しないこと、Runnerが唯一のRunを進行すること、External Search Toolが
query生成・evidence選別を所有しないことを変更しない。
