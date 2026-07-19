# Retrieval dispatch ownership slice 仕様

更新日: 2026-07-20

実装状況: Implemented（PR8）

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md`のPR8を具体化する。

PR7までの移行により、`AnsweringRunner`はcontext preparation、planning、direct / retrieval分岐、
answer phase、result assemblyを所有し、Question Context / Planner / Direct Answer / Evidence Answerは
共通Agent / Runtime境界へ移行済みである。一方、retrieval planを受け取った後のinternal / external /
mixed dispatchとexternal資源scopeだけは、`EvidenceCollectionService` / `ExternalSearchService`の内側に
残っている。

PR8では、retrievalの分岐、固定2枝の並行policy、失敗合成、`EvidenceCollectionOutcome`構築を
回答workflow ownerへ移し、`ExternalResearchRuntimeFactory.activate()`を開始する責任もexternal枝へ
移す。各retrieval capabilityの内部policyは動かさない。

前提はPR0〜PR7の実装完了とする。PR8はPR7完了後の実装をbaselineとし、PR6 / PR7と独立に適用できる
とはみなさない。

### 先行仕様との関係

- `answering-workflow-ownership-slice.md`でPR8へ送ったretrieval dispatch ownershipを本sliceで回収する。
- `external-research-runtime-factory-slice.md`で一時的に`ExternalSearchService`へ置いたactivation ownerを
  本sliceで回答workflowへ移す。factoryの生成・close契約は変更しない。
- `question-answering-parallel-retrieval-slice.md`の両側完走、internal例外優先、side effect維持は継承する。
  同仕様の「external searcher未配線を許容する」「port signatureを変更しない」という移行前の条項は、
  本sliceの非Optional化とper-call resource注入で更新する。
- `evidence-collection-failure-continuation-slice.md`のfailure語彙、canonical order、result assemblyへの写像は
  維持する。ただしproduction到達不能なunconfigured producerは削除する。

### 仕様の固定レベル

本仕様が固定するのは、責任owner、port境界、外から観測できる順序・結果・失敗意味論・資源寿命である。
private method名、helperの個数、`asyncio.gather`そのものの採用、test double名など、同じ契約を満たす
内部構造は固定しない。

## Work Definition

### Problem

- 固定2枝の並行policy——両側完走、internal例外優先、partial result、成功側side effect維持——が
  `EvidenceCollectionService`に埋まり、回答workflow ownerからRunの失敗意味論を追えない。
- external資源のactivation ownerが`ExternalSearchService`の内側にあり、「どのplan分岐がどの資源を
  どのscopeで使うか」を回答workflowから読めない。
- `external_search=None`許容はcompositionのfail-fastによりproductionで到達不能なtest-only seamで、
  mandatoryなexternal capabilityをOptionalに見せている。
- `EvidenceCollectionService`を削除するとprobeと既存seatbeltも移行が必要だが、workflowのproduction
  contractへprobe専用設定や観測hookを追加してはならない。

### Evidence

- `running/answering_runner.py`はretrieval pathで`evidence_collector.collect()`を1回呼び、その内部の
  plan分岐とresource scopeを知らない。
- `evidence_collection/service.py::collect()`はtyped planを3分岐し、internalでは
  `InternalSearchError`だけをfailure値へ変換する。mixedでは両枝を並行実行して全結果を回収し、
  internal、externalの順で未分類例外を選ぶ。
- `collection_failures=["external_search"]`のproduction producerは`external_search=None`経路だけである。
  external pipelineの分類済み失敗は`ExternalSearchOutcome.task_reports`へ格納され、dispatch境界には
  outcomeまたは未分類例外が届く。
- `_build_answering_phases()`と回答開始APIはexternal provider設定をfail-fastするため、productionの
  `AnsweringPhases`でexternal capabilityが欠ける経路はない。
- `ExternalSearchPlan` / `InternalAndExternalPlan`の`external_research_tasks`は1件以上であり、valid planから
  external serviceのtask空早期returnへ到達しない。
- `ExternalSearchService`はagent数丸め、request構築、external runner呼び出し、task横断URL dedupe、
  outcome構築を所有し、現在はrunner呼び出しだけをfactory scope内で行う。
- production compositionは`requested_external_agent_count=None`だが、
  `scripts/probe_question_answering.py --agents`は非`None`値を実際に使用する。agent数丸めは親仕様
  `REG-04`とservice testが保証する既存policyである。
- `test_evidence_collection.py`にはdispatch / failureの非同期seatbelt 15本とDTO validator 5本がある。
  unconfigured producerに依存する非同期testは2本ではなく3本である。

### 責任移動

| 現在の行為 | PR8後のowner | 契約 |
|---|---|---|
| typed retrieval planの3分岐 | 回答workflow owner | internal / external / mixed以外を黙ってfallbackしない |
| internal query値の構築とcapability呼び出し | 同上 | planのquery順を変えず、`InternalSearchError`だけをfailure値へ変換 |
| external capability呼び出し | 同上のexternal枝 | 借用した`ExternalResearchRuntime`をper-callで渡す |
| mixedの固定2枝並行と失敗選択 | 同上 | 両側回収、internal例外優先、成功側side effect維持 |
| `EvidenceCollectionOutcome`構築 | 同上 | DTO validatorとfailure canonical orderを維持 |
| external factory scopeの開始・終了 | 同上のexternal枝 | scope寿命をexternal枝の寿命に一致させる |
| unconfigured external -> failure値 | 削除 | production到達不能な挙動変更として明示的に受け入れる |
| agent数丸め・request / outcome構築・URL dedupe | `ExternalSearchService` | policyと`requested_agent_count`対応を維持 |
| Query -> Tool -> Selector pipeline | `ExternalSearchResearchRunner` | PR9まで変更しない |

## Invariants

### Workflow ownershipとport境界

- retrieval planの分岐、固定2枝の並行、dispatch境界の失敗合成、`EvidenceCollectionOutcome`構築は
  `AnsweringRunner`が所有する回答workflowから追えること。読みやすさのためのprivate helperへの分割は
  許可するが、別のworkflow serviceへ同じ責任を隠さない。
- 回答workflowはcompositionから、少なくともplanner、internal retriever、external searcher、external
  runtime factory、direct answerer、evidence answererを別々に差し替え可能な依存として受け取る。
  現行のhook後・Runごとのlazy dependency構築は維持するが、aggregateのfield名や受け渡し経路は
  永続仕様にしない。旧`evidence_collector` seamは残さない。
- Runner-facing external searcherは、tasks、time window、`as_of`に加えて、external枝が借りた
  `ExternalResearchRuntime`をper-callで受け取る。Runtime / Toolをconstructorやglobal stateへ保持しない。
- 現行`ExternalPlanSearcher`の「runtimeを受け取らず`requested_agent_count`を受け取る」port契約は
  残さない。同じsymbolを新しいRunner-facing契約へ更新するか、役割を表す新portに置き換えるかは
  固定しないが、旧signatureの互換aliasや未使用Protocolを並存させない。
- `ExternalSearchService`はfactoryを保持・activate・closeしない。借用runtimeをexternal runnerへ渡し、
  agent数丸め、request構築、URL dedupe、`ExternalSearchOutcome`構築を引き続き所有する。
- `EvidenceCollectionService`と`EvidenceCollector` Protocolは削除し、互換aliasを残さない。

### `requested_agent_count`とprobe

- `requested_agent_count`を扱う既存のagent数丸めpolicyと`ExternalSearchOutcome`の値は変更しない。
- Runner-facing external searcher portはagent数をworkflow inputとして受け取らない。
  `ExternalSearchService`のconcrete call contractにはoptionalなper-call `requested_agent_count`を残し、
  production compositionは従来どおり既定値`None`で呼ぶ。
- probeの`--agents`、requested / effective count表示、最大3への丸めを維持する。probeはCLI値をbindした
  external searcherをRunnerへ注入し、そのsearcherがserviceのper-call引数へ値を渡す。必要なら同じ境界で
  external outcomeを記録する。exactなadapter名や配置は固定しない。
- probeの都合だけでRunnerのpublic input、`RunContext`、planning contractへagent数やretrieval outcome
  hookを追加しない。collection failure表示は最終resultから、external詳細はprobe-local境界から取得できる。
- probeの`--mode direct`は従来どおりGemini credentialだけを必要とし、DeepSeek / Tavily credentialを
  検査しない。mandatoryなportにはprobe-localのunreachable external searcher / factoryを渡し、
  productionのexternal service / runtime factory builder呼び出しとfactory activationを0回にする。

### 資源scope

- external / mixedのexternal枝だけが`factory.activate()`を開始する。1つのexternal枝につき1回とする。
- 同じ`AnsweringRunner`を複数回使っても、external枝ごとに新しいscope / clientを生成する。
  resourceを共有できる範囲は1つのexternal枝内だけで、別Runや別枝へ持ち越さない(RES-06)。
- context preparation、hook、phases構築、planning、`retrieving` progress通知の失敗、direct path、
  internal-only pathではactivateを0回とする。factory objectの構築とprovider clientの生成を
  同一視せず、clientはactivateまで生成しない。
- scopeは`retrieving` progress後、external capabilityを使う時点で開始し、external枝がresultまたは
  exceptionを合流点へ返す前に終了する。正常なexternal / mixed pathでは`synthesizing` progressと
  Evidence Answer Agent phaseの開始前にclose済みである。
- activation owner移動後のscopeは、Runnerが借用runtimeとともに`ExternalSearchService.search()`を
  呼び出し、serviceがoutcomeを返すまでとする。このため現行のrunner呼び出しだけのscopeから、
  service内の純粋なrequest構築・URL dedupe・outcome構築までresource寿命が限定的に延びることを
  意図的に受け入れる。旧close位置の再現だけを理由にserviceをprepare / finalizeへ分割しない。
- mixedでexternal枝が先に完了した場合、そのscopeはinternal枝を待つためだけに延命しない。
- mixedでexternal枝が失敗した場合も、scope退出完了後の例外を合流する。internal枝も失敗した場合は、
  scope退出後にinternal優先規則を適用する。
- Run cancellation時、dispatch ownerは開始済みのinternal / external両枝をcancelして合流する。
  external pipeline内の子coroutineはそのpipeline ownerがcancel・合流してからexternal枝を終了し、scopeを
  退出する。scope closeが成功した場合は元のcancellationを伝播し、閉じたclientへ到達する孤児coroutineを
  残さない。
- activate / resource構築失敗はexternal枝の未分類失敗として扱う。factoryが部分取得済みresourceを閉じる
  PR4契約は維持する。
- close失敗へretry、shield、独自ExceptionGroup化を追加しない。通常のexternal-only経路と
  mixedの非cancellation合流では、close例外がexternal枝の本体例外を置き換え得るPython既定挙動を
  維持し、mixedではexternal側の未分類例外として通常の優先規則を適用する。
- cancellationとclose失敗の競合は経路ごとに固定する。external-onlyでscopeを直接awaitしている場合は
  close例外がcancellationを置き換え得る。mixedのouter taskがcancelされた場合は、両枝と
  external pipeline内の子処理をcancel・合流し、external childのcleanup例外も回収したうえで、
  通常の失敗選択を行わず元の`CancelledError`を伝播する。outer cancellationを受け取る前に片枝の
  未分類例外が既に完了していた場合も、残る枝との合流待ち中であればcancellationを優先する。
  これは現行のmixed gatherの観測可能な挙動を維持するための意図的な非対称である。
- 上記は1回のouter cancellationとclose試行を対象とする。PR4と同様に、cleanup中の再度の
  cancellationやprocess強制終了までは保証しない。
- valid typed planではexternal taskが1件以上である。旧serviceの「task空ならactivate 0回」は、
  Runnerのvalid-plan保証へ置き換える。validationを迂回したmodelを支えるfallbackは追加しない。

### 失敗意味論

- `InternalSearchError`だけを`collection_failures=["internal_search"]`へ変換する。unknown internal errorを
  failure値へ変換しない。
- external pipelineの分類済みtask / query failureは従来どおり`task_reports`へ格納し、dispatchで
  `collection_failures=["external_search"]`へ変換しない。
- mixedは片側が分類済みfailureでも他側のevidenceを保持する(ERR-07)。
- mixedは片側が未分類例外でも他側の完了を待ち、成功側のcache、metrics、events、task reportsを
  維持する(ERR-08)。
- 両側が未分類例外ならinternal例外を優先し、internalが成功または分類済みfailureならexternal例外を
  伝播する(ERR-09)。選択した非cancellation例外は別instanceへ包み直さない。
- 非cancellation経路のその他の未分類例外を変換・握りつぶしせずworkerまで伝播する(ERR-10)。
  mixedの合流待ち中にouter cancellationを受けた場合だけは、既に完了した未分類例外とcleanup例外を
  drainしたうえでcancellationを優先する。cancellationはfailure outcomeへ変換しない。

### 結果・進捗・観測の同一性

- valid production入力に対する`AnswerQuestionResult`、source順、missing集計、status、citation検証、
  delta / continuation、progress順序を変更しない。例外はunconfigured external producerの削除だけである。
- `EvidenceCollectionOutcome`のDTO / validator、`EvidenceCollectionFailure`語彙、
  `AnswerRetrievalSummary`のAPI shapeを変更しない。`"external_search"` failureのproduction producerは
  無くなるが、DTOの全域、過去結果、result assemblyの写像を維持する。
- `retrieving` progressは最初のretrieval起動前に1回、direct pathでは0回とする。dispatch移動を理由に
  新しいprogress語彙やeventを追加しない。
- retrieval outcomeの正規化、Evidence Answer Agent phase、result assemblyの契約を変更しない。
- `agent_answering_run`、既存Agent phase / provider call spanの名前・attribute・親子関係を変更せず、
  PR8でretrieval専用spanを追加しない。
- `AnswerGenerationStopped`、Direct / Evidence Answerのretry、safe fallback、stream cleanupという
  PR7契約を変更しない。

### 構築・consumer移行

- compositionはinternal retriever、external searcher、external runtime factoryを別々に差し替え可能な
  依存として回答workflowへ供給する。exactなaggregate形状は固定しない。
  `build_external_search_service()`はfactoryをserviceへ埋め込まず、factoryはexternal枝ownerへ供給する。
- external provider credentialの回答開始APIにおけるrun作成前503と、phases構築時のfail-fastを維持する。
  composition helper間の重複check回数は仕様として固定しない。
- production consumer inventoryは`app/agent/running/{contract,answering_runner}.py`、
  `app/agent/{composition,router}.py`、`app/agent/evidence_collection/{contract,service,__init__}.py`、
  `app/agent/evidence_collection/external_search/service.py`、
  `app/agent/answering/evidence_answer/evidence.py`、`app/agent/answering/result_assembly.py`、
  `app/queue/tasks/agent_run.py`、`scripts/probe_question_answering.py`である。
- test consumer inventoryは少なくとも次を含む。
  - `tests/agent/evidence_collection/test_evidence_collection.py`
  - `tests/agent/evidence_collection/external_search/test_service.py`
  - `tests/agent/evidence_collection/external_search/test_research_runner.py`
  - `tests/agent/evidence_collection/external_search/test_runtime_factory.py`
  - `tests/agent/evidence_collection/external_search/test_research_runner_tracing.py`
  - `tests/agent/evidence_collection/external_search/test_external_search_tool_contract.py`
  - `tests/agent/running/test_answering_workflow.py`
  - `tests/agent/running/test_answering_runner.py`
  - `tests/agent/answering/test_orchestration.py`
  - `tests/agent/answering/evidence_answer/test_evidence.py`
  - `tests/agent/test_composition.py`
  - `tests/agent/test_agent_run_task.py`
  - `tests/agent/test_router_research.py`
  - `tests/scripts/test_probe_question_answering.py`
  - `tests/test_lazy_ai_sdk_import.py`
- package re-export、probeのAST seatbelt、composition / workerのlegacy symbol不在検査を新しい境界へ追随させる。
- API / DB / Redis event / Taskiq message / dependencyを変更しない。

## Non-goals

- external pipelineの展開、`ExternalSearchResearchRunner`削除、task / query concurrency、normalization、
  candidate pool、provenance、dedupeのworkflow ownerへの移動(PR9)。
- retrieval phase span・usage観測の追加、部分処理の`Run` / `Runner` naming整理(PR10)。
- `ExternalSearchService` / `InternalSearchService`内部のpolicy、cap、prompt、providerの変更。
- agent数丸め式、hard limit、probe CLIの変更。
- Question Context / Planner / Direct Answer / Evidence AnswerのAgent / Runtime契約再設計。
- `EvidenceCollectionFailure`語彙、`AnswerRetrievalSummary`、公開API response shapeの変更。
- invalidなPydantic model constructionや未配線production graph向けfallbackの追加。

## 固定しない実装詳細

- Runner private methodの名前、個数、引数のまとめ方。
- mixed合流に使う標準library API。ただし両側回収と例外優先規則は固定する。
- required portを`AnsweringPhases`へ直接置くか、同じlifecycleの小さなdependency aggregateへまとめるか。
- probe-local bound / recording adapterのclass名と配置。
- test fileの最終分割、timeline recorderやfakeのclass名。
- external枝scope内における純粋なrequest構築・dedupe処理の細かな位置。ただしactivate条件とclose時点は
  本仕様の資源契約を満たすこと。

## Done

- 回答workflowからinternal / external / mixedの選択、fixed two-branch concurrency、失敗合成、external
  resource scopeを追える。
- `EvidenceCollectionService`、`EvidenceCollector`、`AnsweringPhases.evidence_collector`、
  `ExternalSearchService`のfactory保持が存在せず、定義・import・package re-exportにも残らない。
- 旧`ExternalPlanSearcher`のport signatureが定義・import・package re-exportに残らない。
- production graphではexternal searcherがmandatoryで、unconfigured externalをfailure値へ変換する分岐がない。
- agent数policyとprobeの`--agents`を含め、valid入力の回答結果、progress、error、resource生成有無が
  移行前と同値である。
- external / mixedではexternal枝ごとにresourceを1回activateして全終了経路でcloseし、direct /
  internal-only / retrieval開始前ではactivateしない。
- PR8後のownerに移したseatbeltと既存regression suiteがgreenで、test guarantee ledgerのowner / statusが
  実装結果へ追随している。

## Test contract

実装はowner移動前に既存seatbeltを新しいport境界で再現し、その下で旧ownerを削除する。保証の正本は
最終production ownerへ置き、旧classの内部形状を検査するtestは残さない。
非同期のEvent待機とouter taskは短い`asyncio.wait_for`でboundedにし、直列化や合流漏れを
CI hangではなくtest failureにする。実時間の`sleep`で因果関係を検証しない。

### Dispatchとfailure

- internal / external / mixedごとに選択portだけを呼び、非選択portを0回にする。
- internal queryの値と順序、external tasks / time window / `as_of`、同じborrowed runtimeの伝播を固定する。
- mixedの両枝が実際にoverlapし、片側errorでも他側完了まで待つことをsleepに依存しないeventで検証する。
- classified internal failureを値へ変換し、external evidenceを保持する。
- 失敗statusを含む`ExternalSearchOutcome.task_reports`はexternal / mixedの両経路でそのまま保持し、
  `collection_failures=["external_search"]`を生成しない。mixedではinternal evidenceも保持する。
- unknown internal / external errorの非変換伝播と、両側unknown時のinternal優先を検証する。
  internal-only、external-only、mixedの各選択経路で、伝播した例外がportへ投入したsentinelと
  同一instanceであることを`is`で固定する。
- `EvidenceCollectionOutcome`は、failureの重複禁止・canonical order、internal failureとhitの矛盾禁止、
  external failureとoutcomeの矛盾禁止、および「0件成功はfailureではない」許可契約を維持する。
- unconfigured producerに依存する旧非同期seatbeltは削除または新ownerの有効な保証へ置き換え、
  到達不能なcombined failureをproduction dispatch testとして残さない。

### Resource lifecycle

- context preparation、hook、phases構築、planning、`retrieving` progress通知の各失敗、direct、
  internal-onlyでactivate 0回を検証する。
- external / mixedでexternal枝ごとにactivate 1回とし、borrowed runtime identityがservice / external runnerまで
  変わらないことを検証する。
- 同じRunner / phases factoryを複数Runで使っても、各external枝がfreshなresourceを取得し、
  前Runのruntime / clientを再利用しないことを検証する。
- external正常終了、classified outcome、unknown exception、answer phase failure、Run cancellationでscopeが
  1回closeされることを検証する。answer phase failureではfailure発生前にclose済みであることを確認する。
- mixedのexternal正常終了をEventで先行させ、internalを停止したままexternal scopeのcloseを確認する。
  mixed全体の合流までscopeを延命する実装を拒否する。
- mixedのexternal exception / close failureではscope close後に合流し、internal側も完了してから規則どおり
  例外を選ぶ。activate失敗も同じexternal枝failureとして扱う。
- cancellationでdispatchの両枝がcancel・合流され、external pipeline ownerも自身の子処理を合流し、
  close後のclient利用がないことを既存PR4 / external runner seatbeltと合わせて保証する。
- mixed Runをouter taskからcancelするevent-based testで、開始済みのinternal / external両枝が終了し、
  scope closeも完了してからcancellationが伝播することを検証する。
- mixedの片枝を未分類例外で先に完了させ、もう片枝をEventで停止した状態からouter taskをcancelし、
  先行例外を選ばず元の`CancelledError`を伝播することと、両枝を合流済みであることを検証する。
- external-onlyのcancellation中にscope closeが失敗した場合は、close例外が元の
  `CancelledError`を置き換え、元例外が`__context__`に残る既定挙動を検証する。
- mixedのouter cancellation中にexternal scope closeが失敗した場合は、close試行とexternal childの
  cleanup結果回収を検証し、close例外で置き換えず元の`CancelledError`を伝播する。
- factoryの部分構築失敗、複数client close、close failure既定挙動の正本testはPR4のfactory testに残す。

### Service・composition・probe

- Runner-facing external searcher portはborrowed runtimeを必須で受け取り、`requested_agent_count`を公開しない。
  旧`ExternalPlanSearcher` signatureや重複Protocolがpackage exportに残っていないことを検証する。
- productionのdependency contractでinternal retriever、external searcher、external runtime factoryを
  必須・非Optional・defaultなしとし、構築時の省略を拒否する。`None`判定でunconfigured failure値へ
  fallbackするproduction分岐が存在しないことをcontract / architecture testで固定する。
- `ExternalSearchService`がfactoryを保持・activate・closeせず、借用runtimeをexternal runnerへ渡す。
- agent数丸め、requested / effective count、task空結果、request構築、task横断URL dedupe、outcome validatorの
  既存service regressionを維持する。
- phases構築だけではprovider clientを生成せず、external branch activationまでlazyである。
- public回答開始APIのcredential fail-fast時点と503を維持する。
- external factory objectとcontext manager objectの構築でprovider SDKをloadせず、scope entryまで
  `openai` / DeepSeek具象Runtimeの遅延import契約を維持する。
- probeが削除symbolを参照せず、Runnerと同じcomposition factory経由でexternal資源を使い、`--agents`を
  requested countへ反映して従来のsummaryを表示できる。
- direct probeはDeepSeek / Tavily credentialを要求せず、productionのexternal service / runtime factoryを
  構築・activateしない。probe-localのunreachable dependencyだけを配線する。

### End-to-end regression

- direct / internal / external / mixedの`AnswerQuestionResult`を既存fixtureと同値に保つ(REG-01)。
- citation、missing aspects、collection failures、status capを維持する(REG-02)。
- source、evidence、query、candidate、task reportの順序とcapを維持する(REG-03)。
- requested countを含むeffective task concurrencyを維持する(REG-04)。
- progress `retrieving`がdispatch前に1回、direct pathでは0回であり、`synthesizing`はresource close後である。
- `agent_answering_run`とPR7のAgent / streaming spanに新しいattribute・span・model-visible text露出がない。
- `EvidenceCollectionService` / `EvidenceCollector` / `evidence_collector`の定義・import・re-exportが残存しない。

Exit gate: 親仕様`FLOW-04`〜`FLOW-07`、`ERR-07`〜`ERR-10`、`REG-01`〜`REG-04`、
`RES-01`〜`RES-06`、`ARCH-02` / `ARCH-03`、test guarantee ledger `EC-01`〜`EC-03` / `IS-08` /
`EX-12` / `EX-RES-01`。

残すseam: `InternalSearchService`、`ExternalSearchService`、`ExternalSearchResearchRunner`、
`ExternalResearchRuntimeFactory`、`EvidenceCollectionOutcome`。

削除するseam: `EvidenceCollectionService`、`EvidenceCollector`、unconfigured external許容、
`ExternalSearchService`のfactory activation owner。
