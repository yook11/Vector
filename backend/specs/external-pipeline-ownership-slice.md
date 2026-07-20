# External pipeline ownership slice 仕様

更新日: 2026-07-20

実装状況: Implemented（PR9）

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md`のPR9を具体化する。

PR8までの移行により、回答workflowはretrieval dispatchとexternal資源scopeを所有している。一方、
external枝の内側は依然として4段の伝言——external searcher port -> `ExternalSearchService` ->
`ExternalSearchRunner` port -> `ExternalSearchResearchRunner`——であり、Query Agent phase ->
External Search Tool -> Selector Agent phaseという回答Run内の実際のphase進行は部分Runnerの中に
隠れている。

PR9では、task / query concurrency、query normalization、candidate pool、failure report、
provenance、task横断dedupeの所有を回答workflow ownerへ移し、external pipelineを同じ回答Run内の
private phaseとして展開する。nested Runnerと中継serviceは、pipelineを失った時点で転送だけの
空殻になるため、その呼び出し契約(port・受け渡しDTO)ごと本sliceで削除する(案A、2026-07-19合意)。

移すのは所有と可視性——順序・並行・失敗合成・event / span発火位置——だけとする。「何が正しい
query / pool / evidenceか」を決めるドメイン規則は純関数として`external_search` packageに残し、
Agent宣言、DeepSeek binding、Tool契約、factory契約は変更しない。

前提はPR0〜PR8の実装完了とする。

### 先行仕様との関係

- `retrieval-dispatch-ownership-slice.md`(PR8)のdispatch所有・資源scope契約・cancellation契約は
  そのまま継承する。同仕様の「`ExternalSearchService`を残す」「Runner-facing external searcher
  portがborrowed runtimeをper-callで受け取る」という移行期の条項は、本sliceのport削除で更新する。
  probeの`--agents`契約のうち丸め式と注入は維持し、summary表示は本sliceでsmoke契約へ縮小する
  (PR8のrequested / effective count表示条項を本sliceで更新する)。
- `external-query-selector-agent-runtime-slice.md`(PR2)のAgent宣言・AgentPrompt・DeepSeek binding・
  1:1 runtime instanceは変更しない。
- `external-search-tool-slice.md`(PR3)のTool契約(完成済みqueryのみ実行、candidate返却、
  adapter所有の`external_search_tool_call` span)は変更しない。
- `external-research-runtime-factory-slice.md`(PR4)のfactory生成・close契約、branch scope、
  遅延import契約は変更しない。activate ownerはPR8で確定済みの回答workflowのままとする。

### 仕様の固定レベル

本仕様が固定するのは、責任owner、外から観測できる順序・結果・失敗意味論・資源寿命、
削除inventory、ドメイン規則の意味論である。workflow owner内のprivate method / moduleの分割と
名前、純関数の正確なmodule配置、test fileの最終分割は、同じ契約を満たす限り固定しない。

## Work Definition

### Problem

- Query -> Tool -> Selectorという回答Run内のphase進行、task / query並行、per-taskの失敗合成が
  部分Runnerの中にあり、回答workflow ownerから追えない。移行完了gate `RUN-02`(nested Runnerを
  呼ばない)を満たせない。
- task横断dedupeとoutcome構築が中継serviceに、task並行がnested Runnerに分かれ、external枝の
  policyの置き場が2つに割れている。
- pipelineの所有を移した後の`ExternalSearchService`は丸め呼び出しと引数の束ね直しだけの
  空殻になり、受け渡しDTO(`ExternalSearchRequest` / `ExternalSearchRunResult`)と
  port(`ExternalSearchRunner` / external searcher port)はproducerを失う。空殻を残すと
  「externalの丸めやoutcome構築はserviceの責務」に見える偽のseamになる。

### Evidence

- `external_search/runner.py::search()`の逐条(責任移動表の原資):
  1. `Semaphore(max(1, effective_agent_count))`でtask並行を制限し、全taskを
     `_gather_cancel_on_error`(未分類例外時に兄弟をcancel・合流してから元例外を再送出)で回収する。
  2. per-task: `agent_phase {phase=external_query, agent_name, task_index}` span内でQuery Agentを
     `attempt_number=1`・timeout 30秒で1回呼び、分類済み(`AgentResponseInvalidError` /
     `AIProviderError` / `TimeoutError`)は`query_generation_failed` reportへ変換する。
  3. `_clean_generated_queries`(strip・200字cap・重複排除・3件cap)が空なら
     `query_generation_failed`。非空ならqueries generated eventを発火する。
  4. query別にToolを並行実行(timeout 15秒、`ExternalSearchProviderError` / `TimeoutError`を
     failed query扱い、candidate 10件cap)。全query失敗は`provider_failed` report。
  5. `_build_candidate_pool`(round-robin・URL先勝ち・20件cap) -> candidates fetched event。
     pool空はSelectorを呼ばず`succeeded`(candidate_count=0)。
  6. `agent_phase {phase=external_selector}` span内でSelectorを最大2 attempt・各timeout 30秒。
     分類済み失敗はtyped reason(defect値 / provider reason / `selector_timeout`)へ、finalizeの
     `ValidationError`は`output_schema_mismatch`へ写像し、全attempt失敗は`selector_failed` report。
  7. `_build_evidence`(candidate index参照のみ、範囲外・重複・5件cap超過をdrop、
     `source_ref=external-{task_index}-{candidate_index}`) -> evidence selected event ->
     `succeeded` report(counts / missing付き)。
  8. task順にevidence / reportsを集約して`ExternalSearchRunResult`を返す。
- `external_search/service.py::search()`の逐条: 丸め(`resolve_external_search_agent_count`)、
  `ExternalSearchRequest`構築、runner呼び出し(PR8以降はborrowed runtime転送)、task横断URL先勝ち
  dedupeと`deduplicated_evidence_count`、`ExternalSearchOutcome`構築
  (tasks / requested / effective count含む)。判断を持つのは丸めとdedupeだけで、いずれも移動対象
  または既存純関数である。
- 失敗語彙は契約側にある: `ResearchTaskStatus` 4値と`selector_failure_reason`の写像
  (`_provider_failure_reason`は`reason` StrEnum値 -> `CODE` -> `selector_error`の全域fallback)。
  pipeline移動で語彙・写像は変わらない。
- agent_phase spanは未分類例外のみ`StatusCode.ERROR`を付け、分類済みcatchはspan内で
  reportへ変換されるためspanは正常closeする。attempt span(`agent_provider_call`)はRuntime所有で
  本sliceの対象外。
- 純関数群(`_clean_generated_queries` / `_build_candidate_pool` / `_build_evidence` /
  `ResearchTaskReport.from_raw` / URL dedupe / 丸め式)は「何が正しいか」を決めるドメイン規則で
  あり、`running/`にドメイン規則を置かないPR5の前例(result assembly)に従い`external_search`
  packageへ残す。
- `external_search/agent.py`のAgent宣言はSDK非依存の純粋な宣言であり、workflow ownerからの
  importは遅延import契約(`test_lazy_ai_sdk_import`)を壊さない。
- probeの`--agents`はrequested countとして実効であり(PR8 test contract)、PR8では
  「CLI値をbindしたexternal searcherを注入し、serviceのper-call引数へ渡す」形で維持されている。
  本sliceでそのport自体が消えるため、束縛点の再定義が必要である。
- test consumer: 旧owner境界に依存するのは`test_research_runner.py` /
  `test_research_runner_tracing.py` / `test_service.py`に加え、
  `test_external_search_tool_contract.py`も含む。同testは削除対象の
  `ExternalSearchResearchRunner`と`ExternalSearchRequest`をfixtureに使い、normalization済み
  query / limit / 15秒backstopのworkflow policy(ledger `EX-09`)をRunner経由で検証している。
  owner移動に依存しないのは`test_agent_declaration.py` / `test_tavily.py` /
  `test_runtime_factory.py`である。
- probeの観測seam: 現行probeはevidence collector portを包むrecording adapterで
  `EvidenceCollectionOutcome`を取得し、task reports(status / reason / counts)、全evidence、
  `deduplicated_evidence_count`、effective / hard countまで表示する。`RunResult`は
  `final_output`と`context`しか返さないため、PR8のRunner-facing searcher portが消える本sliceでは
  outcomeを観測できる注入点が無くなる(解決は「`requested_agent_count`とprobe」の節で定める:
  probeをsmoke契約へ縮小し、production側へ観測構造を追加しない)。

### 責任移動

| 現在の行為 | PR9後のowner | 契約 |
|---|---|---|
| task fan-outとconcurrency上限(semaphore) | 回答workflow ownerのexternal枝 | `REG-04`の丸め式による実効並行。external task内だけ上限を使う(FLOW-06) |
| per-taskのQuery -> Tool -> Selector順序 | 同上のprivate phase | `FLOW-05`。planに無いphase / Toolを起動しない |
| Query / Selectorの分類済み失敗 -> task report status | 同上 | status語彙・reason写像・attempt回数・timeout値を維持 |
| 未分類例外の兄弟cancel・合流・伝播 | 同上 | cancellationを非変換。task失敗は他taskをcancelしない(ERR-06) |
| event 3種とagent_phase span 2種の発火 | 同上 | 発火点・payload・per-task因果順序・ERROR条件を維持 |
| task順のevidence / report集約とtask横断URL dedupe | 同上 | 先勝ち・source_ref不変・`deduplicated_evidence_count`一致(REG-06 / 07) |
| `ExternalSearchOutcome`構築 | 同上 | field semantics(requested / effective count含む)を維持 |
| query normalization・pool・evidence構築・report構築・丸め式 | `external_search`の純関数 | 意味論不変。workflow ownerが直接呼ぶ |
| Agent宣言・binding・Tool・factory | 現行owner | 変更しない |
| `ExternalSearchService` / nested Runnerと受け渡し契約 | 削除 | 案A。空殻・偽seamを残さない |

「維持」はバイト同値の意味論維持を指す。表に現れない行為がpipelineに見つかった場合は、
実装中に本仕様へ戻して行き先を決める。

## Invariants

### Workflow ownershipとphase展開

- external枝の進行——丸め、task fan-out、per-taskのQuery Agent phase -> External Search Tool ->
  Selector Agent phase、task順集約、task横断dedupe、`ExternalSearchOutcome`構築——を回答workflowの
  所有するcodeから追えること。読みやすさのためのprivate helper / moduleへの分割は許可するが、
  nested Runnerや中継serviceの形で同じ責任を再導入しない。
- pipelineのどの段からも新しい`RunContext`や永続`AgentRun`を作らない(RUN-03 / RUN-06)。
- typed inputの正方向伝播を維持する(RUN-04): Query Agentのinputはtask・Runと同一の`as_of`・
  `target_time_window`を、Selector Agentのinputはtask・candidate projection・同一の`as_of`を
  受け取る。External Search Toolへ`RunContext`や未使用の`as_of`を渡さない(RUN-05)。
- ドメイン規則の純関数は`external_search` packageに残し、`running/`配下へドメイン規則を
  置かない。timeout定数、`SELECTOR_TIMEOUT_REASON` / `SELECTOR_ERROR_REASON`、
  provider failure reason写像も`external_search` packageに残す(正確なmodule配置は固定しない)。
- Query / Selector Agent宣言、AgentPrompt、DeepSeek binding、`ExternalResearchRuntime`の
  bundle形状、Tool契約を変更しない。borrowed runtimeの利用はPR8が確立したexternal枝scope内に
  閉じる。

### `requested_agent_count`とprobe

- 丸め式`min(task count, max(1, requested if requested is not None else task count), hard limit)`、
  hard limit 3、task 0件時0の既存policyを変更しない(REG-04)。valid planではtaskが1件以上のため、
  丸め結果は常に1以上である。
- requested countはcompositionが回答workflowへ供給するexternal実行依存とし、production既定は
  `None`とする。`RunInput` / `RunContext` / planning contractへagent数を追加しないPR8条項を
  維持する。供給経路のexactな形状(独立引数か小さなpolicy値か)は固定しない。
- probeの役割を実providerでのsmoke検証(起動係)へ縮小する。ロジックの検証はfake注入の
  test suiteを正本とし、失敗診断はLogfireの観測(PR10で完成)で見る。probeへ診断機能を
  戻さない。
- probeのsummary契約は安定チャネル由来だけで構成する: 最終`AnswerQuestionResult`
  (status / answer / sources / collection_failures / missing)と、probeが注入する既存
  `AnswerEventReporter`から得られるper-task進捗(生成query・candidate数・evidence数)。
  `--agents`は同じcomposition factory経由でrequested countとして注入し、丸め式は変更しない。
- `ExternalSearchOutcome`(task report status / reason、dedupe数、effective count)をprobeへ
  露出するための注入点・hook・import可能構造・probe専用引数をproduction側へ追加しない。
  task失敗理由の可視化はPR10の観測完成でspan側に設計する。

### 並行と失敗意味論

- task並行はexternal task内だけで実効並行上限を使い(FLOW-06)、mixedのinternal枝や他のphaseへ
  上限を波及させない。
- Query Agentの分類済みfailure時、そのtaskのTool / Selectorを0回にする(ERR-01)。
- 一部queryのTool failureは他queryとSelectorを既存条件で継続し(ERR-02)、全query failureは
  Selectorを呼ばず`provider_failed` reportを作る(ERR-03)。
- candidate 0件はSelectorを呼ばず`succeeded`を維持する(ERR-04)。
- Selectorの2 attempt、attempt毎timeout、typed failure reasonを維持する(ERR-05)。reason写像は
  全域を保ち、未知のprovider failureは`selector_error`へ落ちる。
- task failureは別taskをcancelしない(ERR-06)。分類済み失敗はreportへ変換されるため
  兄弟cancelの経路に乗らない。未分類例外だけが兄弟cancel・合流の後にworkerまで伝播する。
  cancellationはfailure reportへ変換しない。
- timeout 3定数(query 30秒 / tool 15秒 / selector 30秒)の値と適用位置を変更しない。
- AT-01〜AT-07の分離を維持する: normalization済みqueryだけをToolへ、Toolはqueryを変更せず
  candidateのみ返し、Selectorはcandidate projection(index / title / source name / published at /
  snippet、URL非含有)だけを評価し、outputはcandidate index参照だけでsource metadataを
  pool / task indexから再構築する。structured-output schemaを実行可能Tool一覧へ混入させない。

### 観測とevent

- `agent_phase` span 2種の名前・attribute(`phase=external_query|external_selector`、`agent_name`、
  `task_index`)・親子関係を維持する。未分類例外のみERROR statusを付け、分類済み失敗はspanを
  正常closeする現行条件を変更しない。
- `agent_provider_call`(attempt span)はRuntime所有のまま変更しない。
- event 3種(queries generated / candidates fetched / evidence selected)の発火点・payload・
  per-task因果順序(queries -> candidates -> selection)を維持する。並列taskのglobal total orderは
  要求しない(REG-08)。events reporterの供給はcompositionからworkflow ownerへの注入に変わるが、
  未設定時に発火しない挙動を維持する。
- 本sliceで新しいspan・event・metric・progress語彙を追加しない。per-task spanやusage観測の
  完成はPR10で行う。

### 結果の同一性

- 同一入力に対する`ExternalSearchOutcome`——task順のreports、先勝ちdedupe後のevidence、
  `deduplicated_evidence_count`、requested / effective count——を移行前と同値に保つ。
- `source_ref=external-{task_index}-{candidate_index}`の形式と値を変更しない(provenance)。
  dedupeが残すevidenceのsource refを書き換えない(REG-06)。
- task report / evidence count、task index、dropped selection countの整合を維持する(REG-07)。
- query normalization後のTool call数、generated event、provider failure countを既存値に保つ
  (REG-09)。
- external / mixedの`AnswerQuestionResult`、citation検証、missing集計、status、progress順序を
  変更しない。全task集約後に`synthesizing` progress -> Evidence Answer Agent phaseの順を
  維持する(FLOW-05)。

### 削除と構築の再配置

- 削除inventory(定義・import・package re-export・`__all__`の全てから除去し、互換aliasを残さない):
  - `ExternalSearchResearchRunner`(`external_search/runner.py`本体)。
  - `ExternalSearchService`(`external_search/service.py`本体)と
    `composition.py::build_external_search_service()`。
  - `ExternalSearchRequest` / `ExternalSearchRunResult`(nested呼び出しの受け渡しDTO)。
  - `ExternalSearchRunner` Protocolと、PR8実装後に存在するRunner-facing external searcher port。
    inventoryは名前ではなく「workflowがexternal検索を委譲するport」という意味で行う
    (現行名は`ExternalPlanSearcher`。PR8実装で名前が更新された場合はその最終名を削除する)。
  - `external_search/__init__.py` / `external_search/contract.py`の該当re-export・`__all__`項目。
- `resolve_external_search_agent_count`、純関数群、timeout定数、failure reason写像、
  `ExternalSearchOutcome` / `ResearchTaskReport` / `ExternalSearchEvidence`等の契約型は残す。
- compositionはexternal実行依存(events reporter、requested count既定`None`、factory)を
  回答workflowへ供給する形へ配線を変える。phases構築時のcredential fail-fastと開始APIの
  run作成前503、external branch activationまでのlazy性を変更しない。
- 親仕様の追随修正を本sliceに同乗させる: PR10節の置換対象から本sliceで削除済みのsymbol
  (`ExternalSearchRunResult`等)を外し、PR9節の削除seamへ本sliceの削除inventoryを反映する。
- API / DB / Redis event / Taskiq message / dependencyを変更しない。

## Non-goals

- 部分処理に残る`Run` / `Runner`語の置換、per-task span・usage観測の完成、
  `ExternalSearchOutcome`等の改名(PR10)。
- `run()`の整形とagent視認性のA / B判断(PR10で再訪)。
- Query / Selectorのprompt、model、schema、binding、attempt回数、timeout値の変更。
- Tool契約・Tavily adapter・`external_search_tool_call` spanの変更。
- factoryの生成・close契約、scope境界、遅延import契約の変更。
- cap値(query / candidate / pool / evidence / missing)と丸め式・hard limitの変更。
- internal retrieval、direct path、Question Context / Planner / Answer系の変更。

## 固定しない実装詳細

- workflow owner内のprivate method / moduleの分割と名前(per-task pipelineを1 methodにするか
  helper moduleへ分けるか)。
- 純関数・timeout定数・reason写像の正確なmodule配置(`external_search` package内であること)。
- requested count / events reporterの供給経路のexactな形状。
- semaphoreそのものの採用。ただし実効並行上限とtask非cancelの意味論は固定する。
- test fileの最終分割、fake / recorderのclass名。

## Done

- 回答workflowからexternal枝の全phase進行(丸め -> task fan-out -> Query -> Tool -> Selector ->
  集約 -> dedupe -> outcome)を追え、direct / internal / external / mixedの全pathでnested Runnerを
  呼ばない(RUN-02)。
- `ExternalSearchResearchRunner`、`ExternalSearchService`、受け渡しDTOとportが定義・import・
  re-exportのどこにも残らない。
- 同一入力に対する`AnswerQuestionResult`、`ExternalSearchOutcome`、event / span、progress、
  失敗意味論、資源寿命が移行前と同値である。
- probeが実providerのsmoke検証として動く: `--agents`をrequested countへ注入し、最終結果と
  event由来のper-task進捗だけをsummaryとして表示し、削除symbolやworkflow内部構造を参照しない。
- 移設したseatbeltと既存regression suiteがgreenで、test guarantee ledgerの該当行(少なくとも
  `EX-03`〜`EX-09` / `EX-12`)のowner / statusが実装結果へ追随している。

## Test contract

実装はowner移動前に既存seatbeltを新しい境界で再現し、その下で旧ownerを削除する。保証の正本は
最終production ownerへ置き、旧classの内部形状を検査するtestは残さない。非同期のEvent待機と
outer taskは短い`asyncio.wait_for`でboundedにし、実時間の`sleep`で因果関係を検証しない。

### Pipeline(旧runner seatbeltの移設)

- fake `ExternalResearchRuntime`(fake query / selector runtime + fake tool)を注入したworkflow境界で、
  `test_research_runner.py`の保証を再現する: AT-01(normalization済みqueryのみToolへ)、
  AT-04 / AT-05(Selector inputのcandidate projectionとURL非含有)、AT-06(index参照・範囲外/重複/
  cap超過drop・source metadata再構築)、ERR-01〜ERR-05の各分岐、report status / reason / counts、
  `source_ref`形式。
- Selectorのattempt毎timeout、2 attempt上限、reason写像(defect値 / provider reason /
  `selector_timeout` / `selector_error` fallback、finalize `ValidationError` ->
  `output_schema_mismatch`)を検証する。
- 未分類例外の兄弟cancel・合流をtask階層とquery階層の両方でevent-basedに固定する: 兄弟task /
  兄弟queryがcancelされ、合流(完了)してから同一instanceの元例外が伝播すること、合流完了まで
  external枝のscopeがcloseされないこと、cancellationがreportへ変換されないこと。外側からの
  cancellationでは開始済み全taskがcancel・合流される。
- 旧tool contract test内のworkflow policy保証(`EX-09`: normalization済みqueryとlimitだけを
  typed Tool inputへ渡し、workflow backstop timeoutをclassified failureへ変換する)を
  workflow境界testへ移設し、`test_external_search_tool_contract.py`系にはTool adapter固有の
  契約testだけを残す。

### 並行(FLOW-06 / ERR-06 / REG-04)

- 実効並行上限をevent-basedに検証する: requested countとtask数の組み合わせで同時実行数が
  丸め式を超えない。
- task failure(分類済み・timeout)が別taskをcancelせず、他taskのreport / evidenceが完走する。
- mixed pathでexternal task並行がinternal枝へ波及しない。

### ドメイン純関数

- normalization(strip / cap / 重複 / 件数)、round-robin pool(先勝ちURL / 20件cap)、
  evidence構築(index / drop / 5件cap)、丸め式の単体テストを純関数を直接anchorにして維持する。

### 観測とevent

- `test_research_runner_tracing.py`の保証をworkflow境界で再現する: span名・attribute
  (phase / agent_name / task_index)・分類済み失敗の正常close・未分類のみERROR・親子関係。
  新しいspan / attributeが増えていないこと。
- trace非漏洩を維持する: goal・time window・query・URL・candidate title / snippet・raw model
  output・selection claim / why selectedの各sentinelがtrace dump全体のどこにも現れない。
- 未分類例外のredactionを維持する: export redaction適用下で`exception.message` /
  `exception.stacktrace`が`[redacted]`になり、status descriptionやspan attributeへ生のerror文字列
  ・`result` attributeを漏らさない。
- event 3種の発火点・payload・per-task因果順序。events未設定時に発火しないこと。

### dedupeとoutcome(旧service seatbeltの移設)

- task横断URL先勝ちdedupe、`deduplicated_evidence_count`一致、source ref非書き換え(REG-06)、
  task順report集約とcount整合(REG-07)、outcome field semantics(requested / effective count)。
- `ExternalSearchOutcome` validatorの拒否契約——report数とtask indexの整合(重複・欠番拒否)、
  evidence task indexの範囲、source ref一意性、count accounting——を旧`test_service.py`から
  contract testへ移設し、service test整理で保証を失わない。

### composition・probe・削除検査

- compositionがexternal実行依存を回答workflowへ供給し、phases構築だけではprovider clientを
  生成しない(lazy性維持)。credential fail-fastと開始API 503の時点を維持する。
- probeが`--agents`をrequested countへ注入し、縮小後のsmoke契約(最終結果 + event由来進捗)で
  動作し、削除symbol・workflow内部構造・outcome観測用の裏口を参照しない(AST seatbeltを
  新契約へ追随させる)。
- 削除inventoryの全symbolが定義・import・package re-export・`__all__`から残存しない。
- workflow ownerへのimport追加後もSDK遅延import契約(`test_lazy_ai_sdk_import`)がgreenである。

### End-to-end regression

- external / mixedの`AnswerQuestionResult`を既存fixtureと同値に保つ(REG-01〜REG-03の該当部)。
- 全task集約後に`synthesizing` progressが1回で、resource closeが先行する(FLOW-05、PR8契約)。
- RES-05 / RES-06: external枝の正常終了・Agent failure・Tool failure・answer failure・
  未分類例外・cancellationの全経路でscopeが1回closeされ、Run毎にresourceがfreshである
  (PR8のseatbeltがpipeline展開後も同じ保証でgreenである)。

Exit gate: 親仕様`RUN-02`、`RUN-04`、`FLOW-05` / `FLOW-06`、`AT-01`〜`AT-07`、`ERR-01`〜`ERR-06`、
`REG-04`〜`REG-09`、`RES-05` / `RES-06`。

残すseam: `ExternalResearchRuntimeFactory`、`TavilyExternalSearchTool`とTool契約、
Query / Selector Agent宣言とDeepSeek binding、`ExternalSearchOutcome` / `ResearchTaskReport` /
`ExternalSearchEvidence`等の契約型、ドメイン純関数群。
削除するseam: `ExternalSearchResearchRunner`、`ExternalSearchService`、
`build_external_search_service()`、`ExternalSearchRequest` / `ExternalSearchRunResult`、
`ExternalSearchRunner` / `ExternalPlanSearcher` port、これらのre-export。
