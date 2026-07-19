# Agent テスト保証台帳 + 所有マップ

Status: Draft v0.1 (不変条件群とmodule traceは棚卸し済み / 各PR対象IDの実装可能化は要レビュー)
Scope: `backend/tests/agent/` の test module 62ファイル + runtime共有helper 4ファイル + worker経路
Date: 2026-07-20

## 目的

テスト再設計で、意図された保証を理由なく失わず、偶然の実装詳細を保証として固定しないための
traceabilityを作る。この文書は振る舞い自体の仕様を複製せず、仕様・schema・設定・production・testを
結ぶ移行管理の正本とする。

1. **保証台帳**: 仕様として維持する条件、重複、廃止候補、coverage gap、証拠衝突の目録。単位は
   test関数ではなく、同じproduction ownerと変更理由を持つ不変条件群とする。
2. **所有マップ**: 各不変条件群について、現在のproduction / test ownerと移行後のownerを分けて示す。
   1条件の正本testは1つにするが、別レベルのintegration testが同じ条件を副次的に通ることは許容する。

## Work Definition

### Problem

- file-localなtest doubleと巨大test moduleにより、どのtestがどの不変条件の正本か判別しにくい。
- 既存testだけを移植すると、空虚なtestや実装詳細を保存し、未testの仕様を落とす可能性がある。
- Plannedなworkflow ownership移行があるため、現在と移行後のownerを混ぜると同じtestを再度書き直す。

### Evidence

- 根拠は仕様、Pydantic schema、設定、production、testのすべてから集める。
- 既存testのgreenや既存productionの挙動だけを仕様の根拠にしない。
- 証拠が衝突する場合は推測で優先せず、台帳を`保留`にして衝突元を両方残す。

主要な移行仕様と現在段階:

| 段階 | 仕様 | 状態 |
|---|---|---|
| PR1 Planner Agent / Gemini Runtime | `backend/specs/planner-agent-runtime-slice.md` | Implemented |
| PR2 Query・Selector Agent / DeepSeek Runtime | `backend/specs/external-query-selector-agent-runtime-slice.md` | Implemented |
| PR3 External Search Tool | `backend/specs/external-search-tool-slice.md` | Implemented |
| PR4 External Research Runtime Factory | `backend/specs/external-research-runtime-factory-slice.md` | Planned |
| PR5 AnsweringRunner workflow ownership | `backend/specs/answering-workflow-ownership-slice.md` | Planned |
| PR6 Question Context Agent | `backend/specs/agent-declaration-runner-orchestration-slice.md` | Planned |
| PR7 Direct / Evidence Answer Agent | 同上 | Planned |
| PR8 retrieval dispatch ownership | `backend/specs/retrieval-dispatch-ownership-slice.md` | Implemented |
| PR9 external pipeline ownership | 同上 | Planned |

親仕様の`CTX-* / RUN-* / FLOW-* / AT-* / ERR-* / RES-* / REG-* / OBS-* / ARCH-*`は
`backend/specs/agent-declaration-runner-orchestration-slice.md`のTest matrixを参照する。台帳独自IDは、
この親IDをtest ownerへ割り当てるための細分化であり、新しいdomain仕様を追加しない。

### Invariants

- production、公開API、DB schema、Redis event、Taskiq messageの挙動をこの作業では変更しない。
- `維持`と判断した保証は、旧ownerを削除する前に新owner testをgreenにする。
- testがspan / metric / log / deltaを検証するとき、そのsinkを書くproduction codeを実行する。
- DBの意味を検証するtestはtest DBとproduction repositoryを使い、repository挙動をfakeへ複製しない。

### Non-goals

- production workflow ownershipの先行変更。
- prompt、model、retry、公開payload、永続化 semanticsの変更。
- 全test doubleの一律共通化、またはtest function単位の一覧を恒久的な仕様にすること。

### Done

Draft棚卸しのDone:

- 全test moduleが少なくとも1つの不変条件群または廃止候補へtraceされている。
- 重複、coverage gap、証拠衝突が台帳上で判別できる。

各書き直しPRへ進む前のDone:

- 対象IDごとに根拠、現在のproduction/test owner、移行後owner、差し替え境界、判定を同じschemaで確定する。
- PRが消化するID、追加するreplacement ID、旧testの処置を列挙する。
- `保留`を含むIDはdecision gateを閉じるまで実装へ進めない。

## 判定語彙

| 状態 | 意味 |
|---|---|
| `維持` | 現在有効な仕様根拠があり、owner testを維持する |
| `統合` | 同じ保証が複数testにあり、保証は残して正本を1つにする |
| `移設` | 保証内容は変えず、productionまたはtestの正本ownerだけを移す |
| `置換` | 合意済みの後続仕様で保証自体を変更し、新保証green後に旧保証を閉じる |
| `廃止候補` | 実装詳細または旧ownerだけを固定している疑いがあり、合意後に削除する |
| `gap` | 仕様根拠があるが、非空虚なowner testがない |
| `保留` | 仕様同士、または仕様と現挙動が衝突し、判断が必要 |
| `移行後` | Plannedなownership変更後に有効になる保証またはowner |

## 設計ルール (再設計の合意事項)

1. **1ファイル = 1つのproduction owner + 1つの不変条件群。** 冒頭docstringに「検証するowner /
   所有する不変条件 / 実行する実コード / 差し替える境界」を宣言する。複数outbound portの差し替えや、
   複数実層を通すintegration testは許容する。assertは宣言した不変条件の判定に必要なものに限る。
2. **span を検証するテストは production の span 生成コードを必ず実行する。** 差し替えは span 生成コードの1段下の境界 (SDK client 等) で行う。fake による span 再実装 (`trace_attempts` 型) は禁止。
3. **test doubleは契約・振る舞い・変更理由が一致するときだけ共有する。** 基本語彙は`Scripted*` =
   予定outcome、`Recording*` = 呼び出し記録、`Fake*Client` = SDK / 外部service境界とする。
   concurrency、blocking、constructor、lifecycle等の専用probeを無理に統合しない。共有doubleは契約ownerの
   test packageへ置き、providerや上位policyを再実装しない。
4. **非漏洩testは source data × sink writer × control-flow outcomeで設計する。** 各testは
   「期待するsinkが実在する」「allowlistされた安全な値がある」「禁止sentinelがない」を同時に確認する。
   同じsink writerの分岐はtable化できるが、別sinkを1本のtestへ統合しない。公開deltaは回答本文を許可し、
   internal JSON等を禁止するallowlist channelとして扱う。

## 所有マップ (案・要合意)

現在のowner testをseatbeltとして残し、移行後ownerが同じ保証をgreenにしてから旧ownerを閉じる。
target module名は責任を示す案であり、実装PRで最終確定する。

| prefix | 不変条件群 | 現production owner / 正本test | 移行後production owner / 正本test | 差し替え境界 |
|---|---|---|---|---|
| RT | Runtime契約・safe structured output | `runtime/contract.py`, `_structured_output.py` / `runtime/test_contract.py`, provider test内 | owner不変 / 共通parse保証は`runtime/test_structured_output.py`へ統合案 | なし |
| RT-GM | Gemini one-attempt・provider span | `GeminiAgentRuntime` / `test_gemini*.py` | 不変 | Fake Gemini SDK client |
| RT-DS | DeepSeek one-attempt・provider span | `DeepSeekAgentRuntime` / `test_deepseek*.py` | Runtime owner不変、client lifecycleだけPR4でfactoryへ | Fake DeepSeek SDK client |
| RT-F | FIFO runtime test double | Planner、External Runner、Tool contract testのplain file-local fake | `runtime/_fakes.py::ScriptedAgentRuntime` / `runtime/test_scripted.py` | fake自身。span/scope/concurrencyを含めない |
| PL | Planner宣言・policy・metric・phase | `planning/agent.py`, `service.py` / `planning/test_*.py` | policy owner不変。provider child testだけ実Runtimeへ置換 | Scripted runtime、tracingはFake SDK client |
| QC | Question Context契約・準備policy | `question_context/` / `question_context/test_*.py` | PR6でgeneratorをAgent宣言/Runtimeへ移す。Service policyは残す | Scripted generator、移行後Scripted runtime |
| EC | internal/external/mixed dispatch | `AnsweringRunner` / `running/test_retrieval_dispatch.py` | PR8で移設済み。owner不変 | Recording/Scripted retrieval ports |
| EX | Query→Tool→Selector pipeline | `ExternalSearchResearchRunner` / `test_research_runner*.py` | PR9で`AnsweringRunner` external phaseへ移す | Scripted runtime、Fake Tool。tracingはFake SDK client |
| EX-T | Tavily request・normalization・Tool span | `TavilyExternalSearchTool` / `test_tavily.py`, tool contract test | span/transport owner不変。client lifecycleはPR4 factoryへ | Mock HTTP transport |
| IS | embedding・cache・article search | `internal_search/` / `internal_search/test_*.py` | leaf owner不変。dispatchだけPR8でRunnerへ | SDK fake。DB ownerは実test DB |
| FLOW | direct/retrieval branch・progress | `QuestionAnsweringOrchestrator` / `answering/test_orchestration.py` | PR5で`AnsweringRunner` / Runner timeline・branch testへ移す | Scripted phases、timeline recorder |
| RESULT | citation・missing・status assembly | Orchestrator内の純関数 / orchestration test内 | PR5で`answering/result_assembly.py` / 純関数testへ移す | なし |
| DIRECT | direct stream/policy/live delivery | `DirectAnswerFlow`, Gemini adapter / direct_answer tests | PR7でDirect Agent phase＋streaming Runtimeへ移す | Scripted stream。SDK testはFake Gemini client |
| EVIDENCE | evidence stream/policy/validation | `EvidenceAnswerFlow`, Gemini adapter, domain純関数 / evidence_answer tests | PR7でEvidence Agent phase＋streaming Runtimeへ移す。純関数ownerは維持 | Scripted stream。SDK testはFake Gemini client |
| RUN | outer run・context・hook・run span | `AnsweringRunner` / `running/test_*.py` | PR5でworkflow全体へ拡張 | Scripted preparer/phases。spanは実Runner |
| TH | bounded history・thread projection | `AgentThreadRepository`, projection / `threads/test_repository.py`ほか | 不変 | 実test DB |
| RP | Run lifecycle・completion・race・mapping | `AgentRunRepository`, result mapper / 現在worker巨大file内 | `runs/test_repository_*.py`, `test_result_mapper.py`へ移す | 実test DB |
| WK | persistent attempt coordination | `queue/tasks/agent_run.py` / `test_agent_run_task.py` | owner不変。wiring/outcome/live/logへ分割 | Recording Runner/publisher。repositoryは実物 |
| LIVE | List/Stream/delta/SSE | `live_updates/` / `live_updates/test_*.py` | owner不変 | unit用Redis double＋real Redis integration |
| API | research router・公開schema | router＋Pydantic schema / `test_router_research.py` | owner不変。response/thread/run/contractへ分割案 | enqueue/Redisのみ差し替え、実ASGI＋DB |
| CP | provider resource構築・配線 | `composition.py` / `test_composition.py` | PR4 factory、PR5 phases factoryへ段階的に置換 | SDK client factory / constructor recorder |

未決 (レビューで確定する):

- [ ] WK の分割単位 (配線 / 永続化 / 状態遷移 / log の4ファイル案)
- [ ] runs/test_execution_probe.py の FakeSession 差し替えを維持するか実 DB へ寄せるか
- [ ] 台帳完成後の重複エントリの正本判定 (下記「重複疑い集約」参照)
- [x] `OBS-PV-01`: Gemini / DeepSeekのprovider attempt spanへ固定`prompt_version`を記録し、phaseへは複写しない

---

## 非漏洩マトリクス (集約)

`不在`だけではsink未生成でもgreenになるため、各owner testは安全なsinkの実在とallowlistも確認する。

| ID | source data | sink writer / outcome | 許可する内容 | 禁止する内容 | 正本test / 判定 |
|---|---|---|---|---|---|
| OBS-RUN-01 | question, history, context, previous/final answer | `AnsweringRunner` run span / success・stop・unknown error | `run_id` | model-visible text全般 | `running/test_answering_runner.py` / 維持 |
| OBS-RT-GM-01 | instructions, task input, provider output/error | Gemini provider span / success・blocked・invalid・provider error・unknown | agent/model/attempt/prompt version/result/error/usage | raw input/output/error本文 | `runtime/test_gemini_tracing.py` / 維持 |
| OBS-RT-DS-01 | 同上 | DeepSeek provider span / success・invalid・provider error・unknown | 同上 | 同上 | `runtime/test_deepseek_tracing.py` / 維持 |
| OBS-PHASE-PL-01 | question, repair, draft | Planner phase＋provider child / success・retry・unknown | phase/agent、attempt側のsafe attrs | input/outputとphaseへのusage・prompt version複写 | `planning/test_planner_tracing.py` / 維持 |
| OBS-PHASE-EX-01 | goal, generated query, candidate, selection | Query/Selector phase＋provider child / success・retry・unknown | phase/agent/task index、safe attempt attrs | query/source/model outputとphaseへのusage・prompt version複写 | `test_research_runner_tracing.py` / 維持 |
| OBS-METRIC-PL-01 | question, repair hint | Planner outcome metric / success・retry・fallback | result/retry/mode/fixed failure code | model-visible text | `planning/test_planner.py` / metric assertを1つへ統合 |
| OBS-METRIC-QC-01 | question, history, missing text | Question Context metric・warning / known fallback | fixed result/failure fields | 入力本文・例外本文 | `question_context/test_service.py` / 維持 |
| OBS-METRIC-IS-01 | internal query | Internal Search metric・warning / success・empty・classified failure | result/phase/count | query本文 | `internal_search/test_service.py` / success logはgap候補 |
| OBS-WK-LOG-01 | generation error, unexpected error | worker log / classified・unknown | run ID/error type | exception・question・answer本文 | worker logging test / unknownは維持、classifiedはassert不足のgap |
| OBS-REDIS-LOG-01 | event/delta payload, Redis error | List/Stream/delta publisher log / timeout・exception・breaker open | run/epoch/generation/fixed reason | payload・Redis例外本文 | `live_updates/test_recent_events.py`, `test_stream.py`, `test_answer_delta.py` / 維持 |
| PUBLIC-DELTA-01 | Direct answer | Redis `answer.delta` / success・blank retry | filter済みanswer本文、generation | citation marker/raw provider text | `test_answer_delta_integration.py` / 維持 |
| PUBLIC-DELTA-02〜03 | Evidence answer＋internal JSON metadata | Redis `answer.delta` / success・retry・reset loss | root answer本文、generation/reset | assessment/sufficiency/citations metadata/raw JSON | `test_answer_delta_integration.py` / 維持、producer unitとの重複度は保留 |
| SSE-PUBLIC-01 | typed known live event | SSE serializer / known event | 6種eventの公開schema | raw transport shape | `live_updates/test_sse.py` / 維持 |
| SSE-PUBLIC-02 | untrusted field・unknown event | SSE serializer / injection・unknown | sanitized known fields、fixed metric | unknown raw payload、frame injection | `live_updates/test_sse.py` / 維持 |
| OBS-TOOL-01 | Tavily query/body/key/error | Tool span・error / success・HTTP・transport・decode | provider/model/result/fixed error | query/response body/key/exception本文 | external tool/Tavily tests / 維持 |

`OBS-PV-01`は2026-07-19に解決した。監査eventの代替となる固定識別子としてGemini / DeepSeekの
provider attempt spanへ`agent.prompt.version`を記録し、phase spanへは複写しない。Prompt本文や実行時入力は
引き続き記録しない。

## 他層との重複疑い (集約)

| 対象 | 判定 | 処置案 |
|---|---|---|
| Planner `FakeRuntime(trace_attempts=True)` | provider spanをfakeが再実装 | 解消。`planning/test_planner_tracing.py`で実Gemini Runtime＋Fake SDK clientへ置換済み |
| External `TracingRuntime` | provider spanをfakeが再実装 | 解消。実DeepSeek Runtime＋Fake SDK clientへ置換済み |
| worker内の履歴範囲・順序test | Threads repository ownerと重複 | repository testへ統合し、workerは透過的受渡しだけに狭める |
| worker内のquestion.resolved negative tests | Fake Runnerがproduction hookを再実装 | production `running/test_hooks.py`を正本にしworker側を廃止候補 |
| worker巨大file内のRun repository/race/mapper tests | production ownerと配置が不一致 | `runs/` / `threads/`へ移設 |
| Planner outcome metric asserts | 複数policy testが同じsink contractを再assert | parameterized owner testへ統合 |
| provider別safe structured-output tests | 共通parser保証とadapter接続が混在 | 共通parse保証をRuntime共通testへ寄せ、provider側はmapping接続を確認 |
| 3つの`RecordingDeltaReporter` | 同じportだが記録形・failure injectionが異なる | 共通の最小recording coreだけ統合可。専用failure probeはlocalに残す |
| Evidence assessment非漏洩Redis integration | extractor/flow unitと一部重複するが公開sinkを縦断 | 保留。public sinkの唯一のbehavior testとして残すか、unit ownerだけで十分かPR7時に決定 |
| legacy keyword/import/re-export guard | 移行seatbeltで長期保証とは限らない | 対応移行完了時に廃止判断 |
| compositionのdeferred graph/client保証 | PR4/PR5のtarget仕様と反対 | `移行後`保証を先にgreenにし、同じPRでsupersededとして閉じる |

---

# 保証台帳本体

各行の保証文は索引用の要約であり、根拠欄の仕様・schemaを上書きしない。`現test owner`が複数ある行は、
書き直し時に正本を1つへ決める。

## Runtime

| ID | 保証条件 | 根拠 | 現production owner | 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|---|---|
| RT-01 | `AgentRuntime.invoke`のgeneric typed signatureとprovider-neutral scope factory contractを固定する | PR1、`runtime/contract.py` | Runtime contract | `runtime/test_contract.py` | なし | 維持 |
| RT-INVOKE-01 | provider Runtimeは非正attemptをprovider call前に拒否し、1 SDK callからdeclared typeへvalidation済みoutputを返す | PR1/PR2 | Gemini/DeepSeek Runtime | provider別behavior tests | Fake SDK client | 維持。provider共通conformance群 |
| RT-02 | response defectはnot JSON / not object / schema mismatchの3分類 | PR1/PR2、Runtime contract | Runtime contract | `runtime/test_contract.py` | なし | 維持 |
| RT-03 | parse/validation errorへraw outputやvalidation自由文を残さずsafe repair情報だけを公開 | `_structured_output.py`、PR1/PR2 | shared structured output | provider別Runtime test内 | なし | 統合。`test_structured_output.py`を正本候補 |
| RT-GM-01 | borrowed clientをcloseせずSDKを1回呼び、instructions/input/schema/settingsを分離し、invoke間でstateを持たない | PR1 | `GeminiAgentRuntime` | `runtime/test_gemini.py` | Fake Gemini client | 維持 |
| RT-GM-02 | blocked/known provider/invalidを分類し、unknownはidentity伝播。renderer/config/precondition failureではproviderを呼ばない | PR1 | `GeminiAgentRuntime` | `runtime/test_gemini.py` | Fake Gemini client | 維持 |
| RT-GM-03 | CLIENT provider spanのresult/error/usage/非漏洩とspan非生成経路 | PR1、OBS-02/05/06/08/09 | `GeminiAgentRuntime` | `runtime/test_gemini_tracing.py` | Fake Gemini client | 維持 |
| RT-DS-01 | forced strict function callを1回実行し、bindingはtransport identityだけ、schemaはAgent宣言を正本にする | PR2 | `DeepSeekAgentRuntime` | `runtime/test_deepseek.py` | Fake DeepSeek client | 維持。PR4はclient lifecycleだけ変更 |
| RT-DS-02 | wrong/missing functionとinvalid outputを共通defectへ変換し、known errorを翻訳、unknownをidentity伝播 | PR2 | DeepSeek Runtime＋shared parser | `runtime/test_deepseek.py` | Fake DeepSeek client | 維持、RT-03へ一部統合 |
| RT-DS-03 | DeepSeek provider spanの成功・invalid・schema failure・unknown・renderer failure | PR2、OBS契約 | `DeepSeekAgentRuntime` | `runtime/test_deepseek_tracing.py` | Fake DeepSeek client | 維持 |
| RT-DS-04 | classified provider error spanはprovider_error/safe error type、usage・exception eventなし | PR2 | `DeepSeekAgentRuntime` | `runtime/test_deepseek_tracing.py` | Fake DeepSeek client | 維持 |
| RT-DS-05 | success spanの`gen_ai.operation.name=chat` | PR2 provider-neutral trace | `DeepSeekAgentRuntime` | `runtime/test_deepseek_tracing.py` | Fake DeepSeek client | 維持 |
| OBS-PV-01 | provider attempt spanへ固定Prompt versionを記録し、phaseへ複写しない | audit-removal仕様＋2026-07-19決定 | Gemini/DeepSeek Runtime | provider別tracing＋phase integration tests | Fake SDK client | 維持 |
| RT-F-01 | scripted runtimeはFIFO outcome、call記録、exception identity、説明的なqueue枯渇を提供し、必要なtestだけ明示的に全消費を確認できる | AgentRuntime test support方針 | `runtime/_fakes.py` | `runtime/test_scripted.py` | fake自身 | 維持。unused outcomeの自動failure、span、scope、concurrencyは持たせない |

## Planning

| ID | 保証条件 | 根拠 | 現production owner | 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|---|---|
| PL-01 | requestは`QuestionContext + as_of`のfrozen wrapperで、ServiceはAgentとRuntime scopeへ依存 | PR1 | planning contract/service | `planning/test_contract.py` | なし | 維持 |
| PL-02 | plan variant排他、query/task cap、normalization、draft→completed plan、safe fallback | planning contract/spec | planning contract | `planning/test_contract.py` | なし | 維持 |
| PL-03 | 共通Agentはimmutable/stateless、Plannerのname/prompt/model/schema/versionを1宣言から読める | PR1 | `agent.py`, Planner declaration | `test_planner_agent_declaration.py` | なし | 共通Agent保証とPlanner固有値を統合分離 |
| PL-04 | rendererは全context fieldとsafe previous errorをsanitizeし、wire schemaとdraftが整合する | PR1 | Planner prompt/declaration | declaration test | なし | 維持 |
| PL-05 | response defectだけ1回retryし同一scopeでattempt 1/2。provider errorはfallback、unknown/cancelは伝播 | PR1、FLOW-02 | `QuestionPlanningService` | `planning/test_planner.py` | Scripted runtime＋scope | 維持。PR5後もService owner |
| PL-06 | `plan()`ごとにscopeを1回activateし、別callではfresh scope。実client closeはcomposition owner | PR1、RES-08 | Service＋composition | planner/composition tests | Recording scope / SDK factory | 維持。将来run-local scopeで再評価 |
| PL-07 | response defectだけをretry分類し、repair hintを分類属性へ載せない | PR1 | `planning/failure.py` | `test_planner_failure.py` | なし | 維持 |
| PL-08 | outcome metricを最終結果ごとに1回、low-cardinality属性だけで記録する | audit-removal仕様 | Service/metrics | `test_planner.py`複数 | Scripted runtime | 統合。parameterized ownerへ一本化 |
| PL-09 | 1 phase配下にsuccess 1 / retry 2 provider attempt。phaseへusage/textを複写しない | PR1、OBS-02/05/08 | Service phase＋Gemini Runtime | `planning/test_planner_tracing.py` | 実Runtime＋Fake SDK client | 維持 |
| PL-10 | 旧Planner adapter/exportが存在しない | PR1移行仕様 | package boundary | contract test | なし | 廃止候補。architecture guard価値を判断 |

## Question Context

| ID | 保証条件 | 根拠 | 現production owner | 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|---|---|
| QC-01 | draftをclean/capし、requirement種別・ID・重複・未知fieldを検証する | context仕様/schema | Question Context contract | `question_context/test_contract.py` | なし | 維持 |
| QC-02 | prompt前にhistory本文capとassistant missingのnormalize/dedupe/global capを行い、message対応を保つ | context仕様 | `QuestionContextService` | `question_context/test_service.py` | なし | 維持。message範囲はThreads owner |
| QC-03 | 初回/履歴ありのgenerator呼出しとcontext/telemetry/latest missing flagを決定する | context仕様 | Service | `question_context/test_service.py` | Scripted Context Generator | 維持。PR6でgeneratorだけRuntimeへ移す |
| QC-04 | known generator failure/未構成はsafe fallback、unknownは伝播する | context仕様 | Service | `question_context/test_service.py` | Scripted Context Generator | 維持 |
| QC-05 | question/history/missingをsanitizeしたuntrusted blockへ入れ、非retrieval責任と必須schemaを固定する | context prompt/schema | prompt renderer/schema | `test_gemini_prompt_schema.py` | なし | PR6でAgentPromptへ移設 |
| QC-06 | Gemini request、blocked/error translation、JSON/object/draft validation | current adapterとcontext仕様 | Gemini Context Generator | 専用testなし | Fake Gemini client | gap。PR6 Runtime移行時に保証 |
| QC-07 | outcome metric/warningへquestion/history/missing/answer本文を含めない | context仕様 | Service/metrics | fallback tests | Scripted generator | 維持、sentinel source拡張候補 |

## Evidence Collection

| ID | 保証条件 | 根拠 | 現production owner | 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|---|---|
| EC-01 | internal/external/mixedを選択portへdispatchし、非選択portを呼ばず、mixedは両枝を並行実行する | FLOW-04/06/07 | `AnsweringRunner` | `running/test_retrieval_dispatch.py` | Recording retrieval ports | 維持。PR8でRunnerへ移設済み |
| EC-02 | classified片側failureを値へ変換して成功側根拠を保持し、unknownは両枝合流後に規則どおり伝播する | ERR-07〜10 | `AnsweringRunner` | `running/test_retrieval_dispatch.py` | Scripted ports | 維持。PR8でRunnerへ移設済み |
| EC-03 | failureは固定順・一意で、同じbranchのfailureと成功結果を同時保持しない | collection contract | `EvidenceCollectionOutcome` | `test_evidence_collection.py` | なし | 維持 |

## External Search Agent / Tool

| ID | 保証条件 | 根拠 | 現production owner | 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|---|---|
| EX-01 | Query/Selector typed input/draft、stable Agent宣言、immutable schema、URLなしprojection | PR2、AT-04〜07 | role Agent declaration | `test_agent_declaration.py` | なし | 維持 |
| EX-02 | goal/time window/candidateをsanitizeし、Selector inputにURLを含めない | PR2、AT-05 | AgentPrompt renderer | declaration test | なし | 維持 |
| EX-03 | Queryは1 attempt。classified failure/timeoutでTool/Selectorを短絡し、unknownを伝播。queryをnormalize/capする | ERR-01、AT-01 | External Search Runner | `test_research_runner.py` | Scripted runtime | 維持。PR9でRunner phaseへ移設 |
| EX-04 | Selectorは同じtyped inputで最大2 attempt、classified failureだけretryし、finalization errorをsafe defect化する | ERR-05 | External Search Runner | `test_research_runner.py` | Scripted runtime | 維持。PR9で移設 |
| EX-05 | candidate poolをround-robin、URL先勝ちdedupe、provider/pool capで構築し部分failureを継続する | REG-05/09 | External Search Runner | `test_research_runner.py` | Scripted runtime＋Fake Tool | 維持。PR9で移設 |
| EX-06 | Selector indexからsource metadataを復元し、範囲外/重複/cap超過をdrop・計数する | AT-06、REG-07 | Runner＋contract | `test_research_runner.py` | Scripted runtime＋Fake Tool | 維持。PR9で移設 |
| EX-07 | task並列上限を守り、全task/reportを順序どおり返し、1 task failureを隔離する | ERR-06、REG-04/08 | External Search Runner | `test_research_runner.py` | 専用concurrency probe | 維持。専用fakeは共有しない |
| EX-08 | Query/Selector phaseが実provider attemptを包含し、task indexはphaseだけ、model-visible textをtraceへ出さない | PR2、OBS-02/05/06 | Runner phase＋DeepSeek Runtime | `test_research_runner_tracing.py` | 実Runtime＋Fake DeepSeek client | 維持。PR9でowner移設 |
| EX-09 | Tool portへclean queryとlimitを渡し、workflow backstop timeoutをclassified failureへ変換する | PR3、AT-01/02 | Tool contract＋External Runner | tool contract test内 | Fake/Blocking Tool | 維持。Runner policy testへ移す |
| EX-10 | Tavily固定request/transport timeout/limit clamp/normalization/SafeUrl/error closure/raw body・key非漏洩 | PR3 | Tavily Tool adapter | `test_tavily.py` | Mock HTTP client | 維持。PR4はclient lifecycleのみ |
| EX-11 | Tool CLIENT spanのallowlist、HTTPX child、classified error、cancel、全trace非漏洩 | PR3、OBS契約 | Tavily Tool adapter | external tool contract test | Mock HTTP transport | 維持 |
| EX-12 | effective count、empty短絡、cross-task URL dedupe、report/evidence/source ref accounting | external service仕様 | `ExternalSearchService` | `external_search/test_service.py` | Recording runner | 維持。PR4/8/9でowner段階移行 |
| EX-RES-01 | external branchだけでDeepSeek/Tavily resourceを共有activateし全経路closeする | PR4仕様 | `AnsweringRunner`＋External Research Runtime Factory | `running/test_retrieval_dispatch.py`＋`external_search/test_runtime_factory.py` | Fake SDK factories | 維持。branch scopeとfactory lifecycleで正本を分担 |

## Internal Search

| ID | 保証条件 | 根拠 | 現production owner | 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|---|---|
| IS-01 | queryをstripしempty/cap違反を拒否、実embed文字列からhashを決定する | embedding仕様 | query value objects | query embedding tests | なし | 維持 |
| IS-02 | Gemini embedding requestのtask/model/dimension、empty短絡、欠損/count/error分類 | embedding spec | Gemini Query Embedder | AI embedder tests | Fake Gemini SDK | 維持。将来client ownershipだけ変更 |
| IS-03 | cache key、partial/batch hit、conflict時first vector、halfvec、独立transaction | cache/schema仕様 | Query Embedding Cache | cache tests | 実test DB | 維持 |
| IS-04 | scope/embedding条件、distance＋publish time order、安全なprojection | article search仕様 | PgVector repository | article search tests | 実test DB | 維持 |
| IS-05 | DB operational failure allowlistだけをclassified errorへ変換し、schema/programming errorは伝播する | failure continuation仕様 | PgVector repository | article search tests | 実DB＋failure injection | 維持 |
| IS-06 | cache failureをbest effort化し、missだけembed/store、入力順復元、curation ID dedupe＋最小distance採用 | internal service仕様 | Internal Search Service | internal service tests | Fake embedder/cache/repository | 維持 |
| IS-07 | overall metricを1回、low-cardinality属性だけで記録しwarningへquery本文を出さない | failure仕様 | Service/metrics | internal service tests | Fake collaborators＋実sink | 維持。success log直接確認はgap候補 |
| IS-08 | classified internal failureをretrieval dispatchで継続可能な値へ変換する | continuation仕様 | `AnsweringRunner` | `running/test_retrieval_dispatch.py` | Scripted internal search port | 維持。PR8で変換ownerをRunnerへ移設済み |

## AnsweringRunner / Workflow

| ID | 保証条件 | 根拠 | 現production owner | 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|---|---|
| RUN-CONTRACT-01 | Runのinput/context/resultはtyped immutableで、resultが同じanswering contextを保持する | Runner boundary、RUN-07 | running contract | `running/test_contract.py` | なし | 維持 |
| RUN-HISTORY-01 | 受け取ったprior historyを順序・内容不変のlistとしてContext Preparerへ1回渡す | CTX-01 | `AnsweringRunner` | `test_answering_runner.py` | Scripted Preparer | 維持。bounded範囲はThreads owner |
| RUN-PREVIOUS-01 | latest assistant本文を加工せずprevious answerにし、なければ空文字列 | CTX-02 | `AnsweringRunner` | runner tests | Scripted phases | 維持 |
| RUN-CONTEXT-01 | prepared QuestionContext、RunContext、as_ofを同一identityで後続とresultへ渡し、runごとにfresh context | CTX-01、RUN-03/04/07 | `AnsweringRunner` | runner tests | Scripted Preparer/phases | 維持。PR5で全phaseへ拡張 |
| RUN-HOOK-01 | prepare後hookを1回呼び、original question/has history/same contextだけを渡す | CTX-03 | Runner＋RunHooks | runner contract/hooks tests | Recording event reporter | 維持 |
| RUN-HOOK-02 | historyがありstandalone questionがstrip比較で変化した時だけresolved eventを1回emitする | conversation context仕様 | `running/hooks.py` | `running/test_hooks.py` | Recording reporter | 維持 |
| RUN-ORDER-01 | prepare→hook→answer。prepare/hook failureでは後続0回 | CTX-04、FLOW-01 | `AnsweringRunner` | runner tests | timeline recorder | PR5でfactory/progress/plannerまで拡張移設 |
| RUN-ERROR-01 | unknown exceptionを変換/retryせずidentity伝播する | workflow仕様 | Runner/現Orchestrator | runner/orchestration tests | Scripted failing phase | 維持。PR5で段ごとの後続0回を追加 |
| RUN-STOP-01 | `AnswerGenerationStopped`をidentity再送出しrun spanをerrorにしない | OBS-09 | Runner span | runner span test | Scripted phase＋実span | 維持 |
| RUN-SPAN-01 | `agent_answering_run`がprepare/hook/answer全体を包含する | OBS-01/03 | Runner span | runner tracing tests | span probe collaborators | PR5でworkflow全体へ拡張 |
| RUN-SPAN-02 | run span独自attributeはrun IDだけでmodel-visible textを含めない | OBS-04/05 | Runner span | runner non-leak test | 実span＋Scripted collaborators | 維持 |
| RUN-LEGACY-01 | 旧`context=` keywordを副作用前に拒否する | boundary移行仕様 | Python signature | runner test | なし | 廃止候補 |
| FLOW-BRANCH-01 | direct planはDirectのみ、retrieval planはCollector＋Evidenceのみを起動し非選択portは0回 | FLOW-03〜07 | Orchestrator | `answering/test_orchestration.py` | Scripted ports | PR5でRunnerへ移設 |
| FLOW-PROGRESS-01 | directはplanning→synthesizing、retrievalはplanning→retrieving→synthesizing | workflow仕様 | Orchestrator | orchestration tests | Recording progress | PR5でphaseとの相対順序をtimeline化 |
| FLOW-INPUT-01 | 同じcontext/as_ofを各phaseへ投影し、time windowを必要なpathだけへ渡す | RUN-04 | Orchestrator | orchestration tests | Recording ports | PR5でRunnerへ移設 |
| FLOW-ERROR-01 | planner/collector/answererのunknown exceptionを変換せず伝播する | ERR-10 | Orchestrator | orchestration tests | Scripted failing ports | PR5でRunnerへ移設 |
| FLOW-TIMELINE-01 | prepare→hook→factory→planning progress→planner→branch固有処理の完全timeline | PR5 test contract | 未実装 | なし | single timeline recorder | gap（Planned PR5） |
| FLOW-FACTORY-01 | phases factoryをhook後・run span内で1回、runごとにfresh起動し、構築失敗位置を維持する | PR5仕様 | 未実装 | なし | Scripted phases factory＋実span | gap（Planned PR5） |

## Result Assembly

| ID | 保証条件 | 根拠 | 現production owner | 現test owner | 判定 / 移行後 |
|---|---|---|---|---|---|
| RESULT-DIRECT-01 | direct resultはanswered、source/missing空、planned mode none | REG-01 | Orchestrator | direct branch test | PR5でresult assembly/Runnerへ移設 |
| RESULT-CITATION-01 | unknown citation refを拒否し、実在refだけをevidence順・重複なしでsourceへ射影する | REG-02/03 | Orchestrator純関数 | orchestration tests | PR5で`answering/result_assembly.py`へ移設 |
| RESULT-REQ-01 | unknown requirement IDを拒否し、既知IDをcontext順のmissing文言へ変換する | REG-02 | Orchestrator純関数 | orchestration tests | 同上 |
| RESULT-MISSING-01 | retrieval empty→collection failure→task missing→draft missing→requirement missing順で先勝ちdedupe | workflow仕様 | Orchestrator純関数 | orchestration tests | 同上 |
| RESULT-STATUS-01 | failure/missing/source不足でinsufficientへcapし、answered時missingを空にする | REG-01/02 | Orchestrator純関数 | orchestration tests | 同上 |

## Direct Answer

| ID | 保証条件 | 現production owner / 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|
| DIRECT-CONTRACT-01 | requestとprevious answerを分離し、draft answerはnonblank | direct contract / `test_contract.py` | なし | PR7でAgent input/outputへ移設 |
| DIRECT-PROMPT-01 | question/contextをsanitizeしevidence契約を混ぜず、repair contextを条件付き追加する | prompt/spec / prompt schema tests | なし | PR7でAgentPromptへ移設。意味保証を維持 |
| DIRECT-SDK-01 | stream fragment、block chunk非公開、terminal欠落分類、全経路iterator close | Gemini adapter / AI tests | Fake SDK stream | PR7でstreaming Runtimeへ書換 |
| DIRECT-VISIBLE-01 | chunk分割非依存でcitation markerとouter whitespaceだけを除き、malformed markerを残す | visible filter / stream filter tests | なし | 維持。shared visible-text testへ統合案 |
| DIRECT-POLICY-01 | 最大2 attempt、blankだけretry、provider/unknownはretryせず、safe previous error/metricを使う | Direct Flow / flow tests | Scripted stream generator | PR7でRunner Direct phaseへ移設 |
| DIRECT-LIVE-01 | visible fragmentだけをgeneration付きで通知しresetせず、reporter failureをbest effort化し、stop時abort/close | Direct Flow / flow tests | Scripted generator＋Recording reporter | PR7で実Agent streamを通して維持 |
| DIRECT-REEXPORT-01 | shared stop exceptionとdirect package re-exportのidentity | compatibility seam / flow test | なし | shared identityは維持、re-exportは廃止候補 |

## Evidence Answer

| ID | 保証条件 | 現production owner / 現test owner | 差し替え境界 | 判定 / 移行後 |
|---|---|---|---|---|
| EVIDENCE-CONTRACT-01 | raw draftはlenient、final draftはsufficiency/blank/list制約を持ち、previous answerを渡さない | contract / contract tests | なし | PR7 Agent output/phase inputへ移設 |
| EVIDENCE-NORMALIZE-01 | internal→external順でrefを付け、provenance/public metadataを決定論的に保つ | `evidence.py` / evidence tests | 純粋入力 | 維持 |
| EVIDENCE-PROMPT-01 | context/evidence/time windowをsanitizeしcitation/requirement/JSON schemaを固定する | prompt/spec/schema tests | なし | PR7 AgentPromptへ移設 |
| EVIDENCE-SDK-01 | raw JSON fragmentをparseせずyieldし、blocked本文を隠しterminal/iteration/close規則を守る | Gemini adapter / AI tests | Fake SDK stream | PR7 streaming Runtimeへ書換 |
| EVIDENCE-FINALJSON-01 | root objectだけを受理し全top-level duplicate keyを固定defectで拒否する | `final_json.py` / final JSON tests | なし | 維持。PR7でRuntime parseとの境界再確認 |
| EVIDENCE-VALIDATE-01 | citation/missing/requirement IDをclean/finalizeしdefectを安定分類する | validation / validation tests | 純粋入力 | 維持 |
| EVIDENCE-EXTRACT-01 | root answer stringだけをchunk非依存でdecodeし、他field/JSON syntax/未完成escapeを公開しない | extractor / extractor tests | なし | 維持 |
| EVIDENCE-POLICY-01 | classified failureだけ2 attempt、then safe fallback。unknownは伝播し、metricはlow-cardinality | Evidence Flow / flow tests | Scripted stream generator | PR7 Runner Evidence phaseへ移設 |
| EVIDENCE-REVISION-01 | retry前abort→reset→新generation delta、fallbackも新generation。reporter failureはbest effort | Evidence Flow / flow tests | Recording reporter | PR7へ移設 |
| EVIDENCE-STOP-01 | provider前/midstream/EOF後のstopでparse/metric/fallbackを進めずresourceをcloseする | Evidence Flow / continuation tests | Scripted continuation/generator | PR7へ移設 |

## Core Result Contract

| ID | 保証条件 | 現owner | 判定 / 移行後 |
|---|---|---|---|
| CT-01 | `AnswerQuestionInput`はcontext/as_of/previous answerだけを持ちfrozen・extra禁止 | `agent/test_contract.py` | PR5で型自体を削除予定。意味保証をRunner phase inputへ置換 |
| CT-02 | retrieval summaryがplanned modeとcollection failureを保持する | `test_contract.py` | `answering/test_result_contract.py`へ移設案 |
| CT-03 | internal article ID、external SafeUrl、nonblank evidence claimを検証する | `test_contract.py` | 同じresult contract ownerへ移設案 |
| CT-04 | status/source/missing/collection failureの組合せ制約を守る | `test_contract.py` | 同じresult contract ownerへ移設案 |
| CT-05 | answerとmissing aspectはnonblank | `test_contract.py` | 同じresult contract ownerへ移設案 |

## Shared Answering Request

| ID | 保証条件 | 現owner | 判定 / 移行後 |
|---|---|---|---|
| ANS-REQUEST-01 | `AnsweringRequest`は同じQuestion Contextとas_ofだけを持つfrozen wrapperで、previous answer等の余分なfieldを拒否する | `answering/contract.py` / `answering/test_contract.py` | 維持。PR5でphase requestとしての存否を再確認 |

## Threads / History

| ID | 保証条件 | 現production owner | 現test owner | 差し替え境界 | 判定 |
|---|---|---|---|---|---|
| TH-HIST-01 | 対象threadだけを読む | Threads Repository | `threads/test_repository.py` | 実test DB | 維持 |
| TH-HIST-02 | `seq < current user message`でcurrent user message以降の保存rowをhistory対象から除外する | Threads Repository | `threads/test_repository.py` | 実test DB | 維持 |
| TH-HIST-03 | descending limit後にoldest-firstで返す | Threads Repository | `threads/test_repository.py` | 実test DB | 維持 |
| TH-HIST-04 | 最大limit件だけ返す | Threads Repository | `threads/test_repository.py` | 実test DB | 維持 |
| TH-HIST-05 | assistantだけmissing aspectsをnormalizeして投影する | Threads Repository | `threads/test_repository.py` | 実test DB | 維持 |
| WK-HIST-01 | workerはrepository結果を変更せず、current questionと分けてRunnerへ渡す | worker | worker wiring test | Recording Runner＋実repository | 統合。exact範囲を再assertしない |
| WK-HIST-02 | historyを回答生成前に読み、current outputはRunner完了後にだけpersistするため、同じrunの入力historyへ混入させない。完了済みoutputは次runのprior historyに含める | worker＋Run Repository | follow-up behavior integration | 実DB＋Recording/Scripted answer boundary | 維持 |

## Run Repository / Projection

| ID | 保証条件 | 現test owner | 移行先test owner | 判定 |
|---|---|---|---|---|
| RP-READ-01 | live contextはowned userだけへstatus/epoch/error codeを返す | `test_agent_run_task.py` | `runs/test_repository_read.py` | 移設 |
| RP-CANCEL-01 | cancel winnerがatomic updateで取得したepochを返す | `test_agent_run_task.py` | `runs/test_repository_races.py` | 移設 |
| RP-COMPLETE-01 | assistant/source/run transition/thread updateを同一transactionで完了する | `test_agent_run_task.py` | `runs/test_repository_completion.py` | 移設・worker assertを狭める |
| RP-COMPLETE-02 | citation/source不一致は完了を失敗させずsafe warningだけを出す | `test_agent_run_task.py` | `runs/test_repository_completion.py` | 移設 |
| RP-COMPLETE-03 | terminal先行またはstale epochのcompletionはassistant/source artifactをrollbackする | `test_agent_run_task.py` | `runs/test_repository_races.py` | 統合移設 |
| RP-FAIL-01 | stale workerのmark failedはnew attemptを変更しない | `test_agent_run_task.py` | `runs/test_repository_races.py` | 移設 |
| RP-ACQUIRE-01 | queued/running取得はrunningへ遷移しepochをatomic increment、started/progressを更新する | `test_agent_run_task.py` | `runs/test_repository_lifecycle.py` | 統合移設 |
| RP-ACQUIRE-02 | terminal/missing/transition lossはNoneを返す | `test_agent_run_task.py` | `runs/test_repository_lifecycle.py` | 移設 |
| RP-ACQUIRE-03 | rollback時はepoch incrementも戻り、concurrent acquireは異なるsequenceを得る | `test_agent_run_task.py` | `runs/test_repository_races.py` | 統合移設 |
| RP-ENQUEUE-01 | enqueue failureはepochと独立してqueuedだけをfailedへ変える | `test_agent_run_task.py` | `runs/test_repository_lifecycle.py` | 移設 |
| RP-SWEEP-01 | stale sweepは期限超過active runだけをfailed/staleにする | `test_agent_run_task.py` | `runs/test_repository_lifecycle.py` | 移設 |
| RP-MAP-01 | resultからassistant/source rowへvariant別に写像する | `test_agent_run_task.py` | `runs/test_result_mapper.py` | 移設 |
| TH-PROJ-01 | 保存rowから公開assistant/source unionへ写像する | `test_agent_run_task.py`, API test | `threads/test_projection.py` | 統合移設 |

## Execution Probe / Progress / Citation

| ID | 保証条件 | 現owner test | 判定 |
|---|---|---|---|
| PROBE-01 | 初回query、2秒未満cache、期限後再query | `runs/test_execution_probe.py` | 維持 |
| PROBE-02 | falseをterminal cacheとし再queryしない | `runs/test_execution_probe.py` | 維持 |
| PROBE-03 | DB/session障害をfail-openで短期cacheし、safe log/metricだけを出す | `runs/test_execution_probe.py` | 維持 |
| PROBE-04 | runningかつ同epochだけtrue、cancel/reacquire後の旧probeはfalse | `runs/test_execution_probe.py`＋実DB | 統合維持 |
| RP-EXEC-01 | existence queryはSELECT 1と必要条件だけでcommitしない | probe test内 | Repository query testへ移設 |
| PROG-01 | 同一epochのrunningだけstageを更新しterminal/newer attemptを変えない | `runs/test_progress.py` | 維持 |
| PROG-02 | progress更新失敗は回答を落とさずPII-free warningへ変換する | `runs/test_progress.py` | 維持 |
| CITE-01 | citation marker/source差分を双方向・重複除去後に報告する | `runs/test_citation_integrity.py` | 維持 |

## Worker Task

worker testはproduction repositoryとtest DBを使い、Recording Runnerと外部publisherだけを差し替える。

| ID群 | 保証条件 | 移行先test owner | 判定 |
|---|---|---|---|
| WK-WIR-01〜05 | acquire成功後だけlive/Runner依存を構築し、同じrun/epochを束縛、reset/begin後に回答し、begin failureをbest effort化、skip時は副作用0 | `worker/test_run_task_wiring.py` | 複数testをtimeline/identity中心に統合 |
| WK-WIR-06 | 1回確定したUTC as_ofとresolved hookをRunnerへ渡す | wiring test | 維持。history/current questionはWK-HIST-01が所有 |
| WK-OUT-01〜08 | success completion、classified/internal error mapping、assistant非保存、progress保持、routine stop、new epoch stop | `worker/test_run_task_outcomes.py` | 維持・統合 |
| WK-LIVE-01〜08 | terminalはcommit後、delta finish/revision後にcomplete、breaker継続、failure terminal、race loser/skipはterminalなし、publish failureでDBを戻さない | `worker/test_run_task_live_delivery.py` | 維持・統合 |
| WK-LOG-01 | unknown exception logはrun ID/error typeだけで本文を含まない | `worker/test_run_task_logging.py` | 維持 |
| WK-LOG-02 | classified generation error logも本文を含まない | `worker/test_run_task_logging.py` | gap。現test名に反してlog assertなし |
| WK-HOOK-OLD-01 | echo/fallback/initial contextをpublishしない | workerのFake Runner tests | 廃止候補。production hook testが正本 |
| WK-ARCH-OLD-01 | workerが旧starting-agent seamだけを使う/importする | worker architecture guards | PR5 replacement保証green後に廃止 |

## Research API

| ID群 | 保証条件 | 移行先test owner | 差し替え境界 | 判定 |
|---|---|---|---|---|
| API-START-01〜08 | auth、question validation、credential preflight、202/enqueue、404/409、enqueue不確定時のrun state | `router/test_responses.py` | enqueueのみ＋実ASGI/DB | 維持。DB細部はRepositoryへ移す |
| API-THREAD-01〜05 | own thread list/detail/delete、pagination/order、message/source union、404/204 | `router/test_threads.py` | 実ASGI/DB | 維持。cascade細部はRepository owner |
| API-CANCEL-01〜06 | active cancel、epoch別terminal、terminal failure best effort、completed conflict、ownership 404 | `router/test_runs.py` | Redis publisher＋実ASGI/DB | 維持。DB raceはRepositoryへ移す |
| API-RUN-01〜04 | slim polling response、recent event結合、Redis failure fallback、ownership前Redis非接触 | `router/test_runs.py` | Redis reader | 維持 |
| API-CONTRACT-01 | operation IDs、status code、question cap、slim run fields、enum、message/source union、citation description | `router/test_contract.py` | なし（実OpenAPI/schema） | 統合維持 |

## Composition / Resource Lifecycle

| ID | 保証条件 | 現test owner | 判定 / 移行後 |
|---|---|---|---|
| CP-QC-01 | built Context GeneratorをRunnerへ配線する | `test_composition.py` | 維持・PR6で書換 |
| CP-QC-02 | generator構築のknown config/provider errorはsafe fallback、unknownは伝播 | `test_composition.py` | 維持 |
| CP-PLAN-01 | Planner scopeはlazyでSDK defaultsを使う | `test_composition.py` | 維持 |
| CP-PLAN-02 | normal/abnormal/runtime construction failureでclientを1回closeする | `test_composition.py` | 維持 |
| CP-PLAN-03 | scopeごとにfresh client/runtimeを作る | `test_composition.py` | 維持 |
| CP-EXT-OLD-01 | starting-agent build時はclient/graphを開かず、deferred answerがTavilyを開閉する | `test_composition.py` | PR4/PR5で置換 |
| CP-EXT-OLD-02 | graph/answer failureでもTavilyを解放し、answerごとにfresh clientを作る | `test_composition.py` | PR4 factory保証で置換 |
| CP-EXT-OLD-03 | Query/Selectorが別DeepSeek clientを持ち誰もcloseしない | `test_composition.py` | PR4で明示的に反転。shared client＋factory close ownerへ |
| CP-GRAPH-01 | declared planner/runtime scopeを旧answer graphへ配線する | `test_composition.py` | PR5 phases factory wiringへ置換 |
| CP-LIVE-01 | Direct/Evidenceへ同じdelta/continuation controlを配線する | worker/composition tests | PR5/PR7 phases factory testへ移設 |

## Live Draft / Public Delta

| ID | 保証条件 | 現production / test owner | 差し替え境界 | 判定 |
|---|---|---|---|---|
| LIVE-DRAFT-01 | generationのappend/commit/abort lifecycle。commitだけtail→finish、未commit/例外/cancelはabort、closed session再利用拒否 | `answering/live_draft.py` / `answering/test_live_draft.py` | Recording reporter | 維持。PR7でも実productionを通す |
| PUBLIC-DELTA-01 | Direct delta連結がfilter済みfinal answerと一致し、blank retryはgeneration 2のみでresetしない | Direct Flow→Redis integration | Fake stream generator | 維持 |
| PUBLIC-DELTA-02 | Evidence deltaはroot answerだけを含みassessment/sufficiency/ref/missing/raw JSONを含めない | Evidence Flow/extractor→Redis integration | Fake stream generator | 維持。unitとの重複度は保留 |
| PUBLIC-DELTA-03 | Evidence retryはreset→new generation delta、reset喪失時もhigher generationを独立配信する | Evidence Flow→Redis integration | fault wrapper | 維持 |
| PUBLIC-DELTA-04 | delta breakerがstage/activity/terminal/List producerを壊さない | real Redis integration | delta専用failing wrapper | 維持 |

## Redis List / Stream / Delta Reporter

| ID | 保証条件 | 現production / test owner | 差し替え境界 | 判定 |
|---|---|---|---|---|
| LIVE-LIST-01 | recent eventsのcap/TTL、latest Nをoldest-first、bad entry skip、resetはListだけ削除 | `recent_events.py` / `test_recent_events.py` | Redis double＋real Redis | 維持 |
| LIVE-LIST-02 | publish/read/reset timeout・exceptionをbest effort化しpayload/error本文をlogへ出さない | `recent_events.py` / `test_recent_events.py` | Raising/Hanging Redis | 維持 |
| LIVE-FANOUT-01 | stageはDB＋Stream、activityはList＋Streamへ独立試行し、一方のfailureで他方を省略しない | `reporters.py` / `test_reporters.py` | Recording/raising sinks | 維持 |
| LIVE-FANOUT-02 | nested activity shapeを保持し、adapter自身がpayload/exception本文をlogしない | `reporters.py` / `test_reporters.py` | throwing sinks | 維持。sink別warningの正本はpublisher側 |
| LIVE-STREAM-01 | typed 6 event、正attempt epoch、bounded TTL/MAXLEN/page/timeout、nested activity envelope | `stream.py` / `test_stream.py` | memory＋real Redis | 維持 |
| LIVE-STREAM-02 | payload decode前にepochを分類し、old skip/current return/new boundary非消費を守る | `stream.py` / `test_stream.py` | memory Redis | 維持 |
| LIVE-STREAM-03 | bad envelope/payloadだけskipし、absent/advanced/trimmed/unavailableを区別してcursorを進める | `stream.py` / `test_stream.py` | memory Redis | 維持 |
| LIVE-STREAM-04 | begin timeout後のlazy marker retryとsame-epoch duplicate markerを許容する | `stream.py` / `test_stream.py` | failure Redis＋real Redis | 維持 |
| LIVE-STREAM-FAIL-01 | publisher failure→None、reader failure→unavailable、logical timeout内復帰、payload/error非漏洩 | `stream.py` / `test_stream.py` | Raising/Delayed Redis | 維持 |
| LIVE-DELTA-COALESCE-01 | time/size threshold、順序、timer競合でloss/dup/reorderなし、abortでpending破棄 | `answer_delta.py` / `test_answer_delta.py` | Scripted publisher＋manual clock | 維持 |
| LIVE-DELTA-BREAKER-01 | reset/delta共通counter、連続failureでopen、successでreset、attemptごとfresh、open後timer回収 | `answer_delta.py` / `test_answer_delta.py` | Scripted publisher | 維持 |
| LIVE-DELTA-BREAKER-02 | breaker log/metricはrun/epoch/generation/fixed reasonだけでpayload/error本文なし | `answer_delta.py` / `test_answer_delta.py` | failing publisher＋実sink | 維持 |

## SSE / Live Endpoint

| ID | 保証条件 | 現production / test owner | 差し替え境界 | 判定 |
|---|---|---|---|---|
| SSE-PUBLIC-01 | typed 6 eventをallowlist投影し、Stream ID/type/camelCase single data lineへserializeする | `sse.py` / `test_sse.py` | typed entries | 維持 |
| SSE-PUBLIC-02 | CR/LF/NUL/frame injectionを防ぎ、unknown eventのraw payloadを出さずfixed metricだけを記録する | serializer tests | fake entry＋実metric | 維持 |
| SSE-CAPACITY-01 | process/run/user capをatomic取得し全exitでrelease、lease expiryとresponse-start failureを処理する | `sse.py`, `sse_response.py` / SSE tests | fake clock | 維持 |
| SSE-REPIN-01 | epoch advanceでboundary cursorを保って同接続re-pinしeventを落とさない | SSE tests | Scripted reader＋clock | 維持 |
| SSE-DEGRADE-01 | absent grace、unavailable close、zombie cursor advance、trim時の不完全draft抑止 | SSE tests | Scripted reader＋clock | 維持 |
| SSE-LIFECYCLE-01 | retry frame先行、IDなしheartbeat、fixed deadline、terminal close、disconnect/cancel cleanup | SSE tests | fake clock/reader | 維持 |
| SSE-QUEUED-01 | epoch 0ではRedisを読まずDB transitionを待ち、terminalはsynthetic eventなしで終了する | SSE tests | context loader＋reader | 維持 |
| SSE-HTTP-01 | auth→validation→ownership→Redis、404/204、safe headers、preflight error mapping | `test_router_sse.py` | dependency override＋実ASGI | 維持 |
| SSE-HTTP-02 | middleware disconnectでもreader cancel/capacity releaseし、real Redis eventをFastAPI SSEまで届ける | router integration | app stack＋real Redis | 維持 |

## Planned replacementの閉じ方

`移行後`の保証は現在の保証に上書きして先に消さない。各ownership PRで次の順に扱う。

1. replacement IDと根拠仕様を台帳へ追加する。
2. 新production ownerを実行するtestをredで追加する。
3. production移行後に新owner testをgreenにする。
4. 同じPRで旧IDを`superseded by <ID>`として閉じる。
5. 旧owner testと旧test doubleを削除する。

この順序により、意図しない保証喪失と合意済み設計による保証置換を区別する。

## Test module traceability

glob単位で全62 test moduleを台帳IDへ対応づける。個別PRでは、対象glob内のtest functionを消化する
台帳IDへさらに紐づけ、未分類のまま旧fileを削除しない。

| 現test module | 台帳ID | 主な処置 |
|---|---|---|
| `runtime/test_contract.py` | RT-01/02 | Protocol/defect契約を維持 |
| `runtime/test_scripted.py` | RT-F-01 | 共有FIFO fakeの契約を維持 |
| `runtime/test_gemini.py`, `test_gemini_tracing.py` | RT-INVOKE-01, RT-03, RT-GM-*, OBS-PV-01 | 共通parseを統合、span owner維持 |
| `runtime/test_deepseek.py`, `test_deepseek_tracing.py` | RT-INVOKE-01, RT-03, RT-DS-*, OBS-PV-01 | 共通parseを統合、provider error/operation/prompt version保証を維持 |
| `planning/test_contract.py` | PL-01/02/10 | domain contract維持、legacy guard判断 |
| `planning/test_planner_agent_declaration.py` | PL-03/04 | 共通Agent保証とrole固有保証を分離 |
| `planning/test_planner.py`, `test_planner_tracing.py` | PL-05/06/08/09 | policyはscripted Runtime、親子spanは実Runtime＋Fake SDK client |
| `planning/test_planner_failure.py` | PL-07 | 維持 |
| `question_context/test_contract.py` | QC-01 | 維持 |
| `question_context/test_service.py` | QC-02/03/04/07 | Service policy維持、fake改名候補 |
| `question_context/test_gemini_prompt_schema.py` | QC-05 | PR6 Agent移行のseatbelt。QC-06は専用testなしのgap |
| `evidence_collection/test_evidence_collection.py` | EC-03 | DTO validator正本を維持 |
| `evidence_collection/external_search/test_agent_declaration.py` | EX-01/02 | 維持 |
| `evidence_collection/external_search/test_research_runner.py` | EX-03〜07 | PR9 seatbelt、plain Runtime fakeだけ共有 |
| `evidence_collection/external_search/test_research_runner_tracing.py` | EX-08 | 実Runtime＋Fake SDK clientで維持 |
| `evidence_collection/external_search/test_external_search_tool_contract.py` | EX-09/11 | plain Runtime fakeを共有し、Tool spanを残す |
| `evidence_collection/external_search/test_tavily.py` | EX-10 | 維持 |
| `evidence_collection/external_search/test_service.py` | EX-12 | `ExternalSearchService` policy正本を維持 |
| `evidence_collection/external_search/test_runtime_factory.py` | EX-RES-01 | factoryの部分構築 / close正本 |
| `evidence_collection/internal_search/ai/test_gemini_query_embedder.py`, `test_gemini_query_embedding_spec.py` | IS-02 | 維持 |
| `evidence_collection/internal_search/test_query_embedding.py`, `test_query_embedding_identity.py` | IS-01 | 統合候補 |
| `evidence_collection/internal_search/test_query_embedding_cache.py` | IS-03 | 実DB owner維持 |
| `evidence_collection/internal_search/test_article_search.py` | IS-04/05 | 実DB owner維持 |
| `evidence_collection/internal_search/test_service.py` | IS-06/07/08 | 維持 |
| `answering/test_contract.py` | ANS-REQUEST-01 | phase request contract維持、PR5で存否確認 |
| `answering/test_live_draft.py` | LIVE-DRAFT-01 | production session owner維持 |
| `answering/test_orchestration.py` | FLOW-*, RESULT-* | PR5でRunnerとresult assemblyへ分割移設 |
| `answering/test_result_assembly.py` | RESULT-MISSING-01/RESULT-STATUS-01 | historical external failureのresult assembly保証 |
| `answering/direct_answer/test_contract.py` | DIRECT-CONTRACT-01 | PR7 seatbelt |
| `answering/direct_answer/test_stream_filter.py` | DIRECT-VISIBLE-01 | shared visible-text ownerへ統合候補 |
| `answering/direct_answer/test_flow.py` | DIRECT-POLICY/LIVE/REEXPORT | PR7 seatbelt、compat guard判断 |
| `answering/direct_answer/ai/test_gemini.py`, `test_prompt_schema.py` | DIRECT-SDK/PROMPT | PR7 Runtime/Agent移行seatbelt |
| `answering/evidence_answer/test_contract.py` | EVIDENCE-CONTRACT-01 | PR7 seatbelt |
| `answering/evidence_answer/test_evidence.py` | EVIDENCE-NORMALIZE-01 | domain純関数owner維持 |
| `answering/evidence_answer/test_final_json.py` | EVIDENCE-FINALJSON-01 | 維持、PR7でparse境界再確認 |
| `answering/evidence_answer/test_json_answer_extractor.py` | EVIDENCE-EXTRACT-01 | 維持 |
| `answering/evidence_answer/test_validation.py` | EVIDENCE-VALIDATE-01 | 維持 |
| `answering/evidence_answer/test_flow.py` | EVIDENCE-POLICY/REVISION/STOP | PR7 seatbelt |
| `answering/evidence_answer/ai/test_gemini.py`, `test_prompt_schema.py` | EVIDENCE-SDK/PROMPT | PR7 Runtime/Agent移行seatbelt |
| `running/test_contract.py` | RUN-CONTRACT/HOOK | 維持、PR5 signature更新 |
| `running/test_answering_runner.py` | RUN-* | PR5でworkflow ownerへ拡張 |
| `running/test_retrieval_dispatch.py` | EC-01/02, EX-RES-01, IS-08 | `AnsweringRunner` dispatch / failure / resource scope正本 |
| `running/test_hooks.py` | RUN-HOOK-* | production hook正本維持 |
| `threads/test_repository.py` | TH-HIST-* | 実DB owner維持、worker重複を吸収 |
| `runs/test_execution_probe.py` | PROBE-*, RP-EXEC-01 | probe policy維持、repository queryを移設候補 |
| `runs/test_progress.py` | PROG-* | 維持 |
| `runs/test_citation_integrity.py` | CITE-01 | 維持 |
| `test_contract.py` | CT-* | result contract移設、AnswerQuestionInputはPR5置換 |
| `test_agent_run_task.py` | WK-*, RP-*, TH-HIST, RP-MAP, TH-PROJ | worker4責任＋repository/projectionへ分解 |
| `test_composition.py` | CP-* | PR4/5 replacementとstable Planner/QC wiringを分離 |
| `test_router_research.py` | API-* | responses/threads/runs/OpenAPIへ分割 |
| `live_updates/test_answer_delta.py` | LIVE-DELTA-* | 維持 |
| `live_updates/test_answer_delta_integration.py` | PUBLIC-DELTA-* | behavior integration維持、重複度はPR7で判断 |
| `live_updates/test_recent_events.py` | LIVE-LIST-* | 維持 |
| `live_updates/test_reporters.py` | LIVE-FANOUT-* | 維持、sink非漏洩正本を分離 |
| `live_updates/test_stream.py` | LIVE-STREAM-* | unit＋real Redis owner維持 |
| `live_updates/test_sse.py` | SSE-PUBLIC/CAPACITY/REPIN/DEGRADE/LIFECYCLE/QUEUED | 維持 |
| `live_updates/test_router_sse.py` | SSE-HTTP-* | auth/ASGI integration owner維持 |

共有helper `runtime/_fakes.py`はRT-F-01の正本、`runtime/_helpers.py`と
`runtime/_deepseek_helpers.py`はRT-GM/RT-DSのSDK boundary data/builders、`runtime/_tracing_helpers.py`は
provider spanの観測だけを所有する。
