# Answering workflow ownership slice 仕様

更新日: 2026-07-19

実装状況: Implemented（PR5）

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md`のPR5を具体化する。

`starting_agent.answer()`の内側に隠れている回答workflowの本体——planning、direct / retrieval分岐、
answer phase、result assembly、progress発火——を`AnsweringRunner.run()`から追える形へ移し、
`QuestionAnsweringOrchestrator`、workflow全体を表す`QuestionAnsweringAgent`、`starting_agent`を
削除する。

移すのはworkflowの骨格(順序・分岐・発火・assembly)だけとする。各phaseの中身は注入portのまま
動かさない: planning policyは`QuestionPlanningService`、retrieval dispatchは
`EvidenceCollectionService`、answer生成は`DirectAnswerFlow` / `EvidenceAnswerFlow`に残る。

前提: PR1〜PR4の実装完了。external factoryのactivation ownerは`ExternalSearchService`のまま
動かさない(PR8)。

## Work Definition

### Problem

- `AnsweringRunner.run()`から見える回答処理はcontext preparation、hook、`starting_agent.answer()`、
  `RunResult`構築までであり、どのAgent / 検索能力がどの順序で起動するかを読み取れない。
- workflow全体が`answer()`だけの`QuestionAnsweringAgent`としてAgent語彙を占有し、PR1以降の
  「Agent = 1つのLLM役割の宣言」と衝突している。
- progress発火(planning / retrieving / synthesizing)とresult assemblyがorchestratorに埋まり、
  workflow ownerとspan境界(`agent_answering_run`)の所有者が別のobjectになっている。

### Evidence

- `running/answering_runner.py::run()`はcontext preparation -> `AnsweringRunContext`構築 -> hook ->
  `starting_agent.answer()` -> `RunResult`の順で、`AnswerGenerationStopped`だけをspan終了後に
  同一instanceで再raiseする。
- `composition.py::_DeferredQuestionAnsweringAgent`が`answer()`時に回答graphを遅延構築する
  (PR4適用後はexternal資源がfactory化された残余の構築のみ)。この構築はcontext preparationと
  hookの後、かつ`agent_answering_run` spanの内側で走り、構築時に設定検証
  (`ensure_question_answering_agent_configured`)とGemini系clientの生成が発生する。
  構築位置を`run()`前へ前倒しすると、構築失敗がhookもspanも無い場所で起きる挙動変更になる。
- `composition.py::build_question_answering_agent()`が削除対象の`QuestionAnsweringAgent`を返し、
  `app/agent/__init__.py`と`answering/__init__.py`が旧symbolをre-exportしている。
- `scripts/probe_question_answering.py`がOrchestratorと`AnswerQuestionInput`を直接利用し、
  固定plannerを注入する貫通probeとして存在する。
- Runnerが依存する`EvidenceCollector` Protocolは、削除対象の`orchestration.py`内に定義されている。
- `answering/orchestration.py::QuestionAnsweringOrchestrator.answer()`の逐条は以下のとおり
  (次節のマッピング表の原資)。progress reporterがNoneなら発火しない。
  1. progress `planning`を発火する。
  2. `PlanningRequest` / `AnsweringRequest`を`QuestionContext`と`as_of`から構築する。
  3. `planner.plan()`を呼び、typed planを受け取る。
  4. planを`match`し、`NoRetrievalPlan`ではprogress `synthesizing` -> `direct_answerer.answer()`
     (previous_answer付き) -> `status="answered"`・sources空のresultを構築する。
  5. retrieval系planではprogress `retrieving` -> `evidence_collector.collect()` ->
     `normalize_answer_evidence()` -> progress `synthesizing` -> `evidence_answerer.answer()`
     (target_time_window付き)。
  6. `_validate_draft_citations()`が未知citation refを`EvidenceAnswerDraftInvalidError`にする。
  7. `_unfulfilled_requirement_missing_aspects()`が未知requirement idを同errorにし、
     missing文言へ写像する。
  8. `_sources_for_citations()`がcited_refsからsourcesを射影する。
  9. `_assemble_evidence_result()`がmissing集計(retrieval empty / collection failure写像 /
     external task missing / draft missing / requirement missing -> dedupe)とstatus導出
     (answered / insufficient、answered時missing空化)を行う。
- direct pathではprogress `retrieving`を発火しない。

### 逐条マッピング表(orchestratorの行為 -> 移設先)

| 現行の行為 | 移設先 | 変更 |
|---|---|---|
| progress `planning`発火 | `AnsweringRunner`(hook後、planning phase前) | 位置のみ |
| `PlanningRequest` / `AnsweringRequest`構築 | Runner private | なし |
| `planner.plan()`呼び出し | Runnerのplanning phase(注入serviceを呼ぶ) | なし |
| typed planの`match`分岐 | `AnsweringRunner.run()`本体 | なし |
| direct path一式(progress / answer / result) | Runner private(`_answer_directly`相当) | なし |
| retrieval path一式(progress / collect / normalize / answer) | Runner private | なし |
| citation検証・requirement検証・sources射影 | `answering/result_assembly.py`へ純関数のまま移設 | ロジック不変 |
| missing集計・status導出・result assembly | 同上。Runnerはassemblerを1回呼ぶだけ | ロジック不変 |
| progress reporter保持と発火helper | Runner | なし |
| 回答graph(phase依存)の遅延構築 | composition実装のlazy phases factory。Runnerがhook後・span内で起動 | 位置の所有者のみ |

「変更なし」はバイト同値の意味論維持を指す。マッピングに現れない行為がorchestratorに
見つかった場合は、実装中に本仕様へ戻して行き先を決める。

### Invariants

#### Workflow ownership

- ユーザー入力から最終outputまでのphase / capabilityの起動順と分岐は`AnsweringRunner`が所有し、
  `run()`(と読みやすさのためのprivate method)から高レベル順序を追える。
- `QuestionPlanningService`、`EvidenceCollectionService`、`DirectAnswerFlow` /
  `EvidenceAnswerFlow`は注入portのまま維持し、内部のretry / fallback / policy / streamを
  本sliceで変更しない。phase policyをRunnerのprivate methodへ畳む判断はPR10で再訪する。
- Runnerの依存契約を次で固定する。phase依存は宣言済みの既存Protocolで受け、具象を知らない。

```python
@dataclass(frozen=True, slots=True)
class AnsweringPhases:
    planner: QuestionPlanner              # planning/contract.py(既存)
    evidence_collector: EvidenceCollector  # evidence_collection/contract.pyへ移設
    direct_answerer: DirectAnswerer        # direct_answer/contract.py(既存)
    evidence_answerer: EvidenceAnswerer    # evidence_answer/contract.py(既存)


class AnsweringPhasesFactory(Protocol):
    def __call__(self) -> AnsweringPhases: ...
```

- `AnsweringRunner`のconstructorは`context_preparer`、`phases_factory: AnsweringPhasesFactory`、
  `progress: AnswerProgressReporter | None`を受け取る。`EvidenceCollector` Protocolは削除対象の
  `orchestration.py`から`evidence_collection/contract.py`へ移設する。
- phase依存の構築(graph遅延構築の残余)は、Runnerが`run()`内のhook成功後・`agent_answering_run`
  span内で`phases_factory()`を1回呼ぶことで行う。構築失敗(設定検証・client生成)の発生位置・
  伝播・観測は現行の遅延構築と同値に保つ。`AnsweringPhases`はRunごとにfactoryで生成し、
  Run間で再利用しない。
- mixed retrievalの並行policy、partial failure、external factoryのactivationは
  `EvidenceCollectionService` / `ExternalSearchService`の中に残す(PR8)。
- `AnsweringRunner.run()`のsignatureから`starting_agent`引数を除去する。`RunInput`、`RunContext`、
  `RunResult`、`RunHooks`の型は変更しない。

#### 挙動の同一性

- progress発火は位置・回数・順序を維持する: `planning`はhook完了後かつplanning phaseの最初の
  attempt前に1回、`retrieving`は最初のretrieval前に1回(direct pathでは発火しない)、
  `synthesizing`はanswer phase前に1回。
- context preparationまたはhookの失敗時は、planner以降のphase / capabilityを1つも起動しない
  (現行の`starting_agent.answer()`不呼び出しと同値)。
- citation検証、requirement検証、sources射影、missing集計、status導出は
  `AnswerQuestionResult`の意味を決めるドメイン規則であり、workflowの順序とは変更理由が異なる。
  既存の純関数をロジック不変で`answering/result_assembly.py`へ移設し、Runnerはassemblerを
  1回呼ぶだけとする。`running/`配下へドメイン規則を置かない。同一入力に対する
  `AnswerQuestionResult`の出力は同値になる。
- `AnswerGenerationStopped`は同一instanceのままworkerへ伝播し、`agent_answering_run`を
  error扱いしない現契約を維持する。未分類例外は変換せず伝播する。
- `RunContext` / `run_id` / `as_of`は回答Runで1回だけ生成し、`QuestionContext`は1回準備して
  後続phaseへ同じinstanceを渡す。
- `agent_answering_run` spanの名前・attribute(`run_id`のみ)・成功失敗記録を変更せず、
  本sliceで新しいspanを追加しない。

#### 削除と構築の再配置

- `QuestionAnsweringOrchestrator`、`QuestionAnsweringAgent` protocol、
  `_DeferredQuestionAnsweringAgent`、`build_question_answering_starting_agent()`、
  `build_question_answering_agent()`を削除する。互換aliasは残さない。
- `app/agent/__init__.py`と`answering/__init__.py`の旧symbol re-exportを同時に更新し、
  package importを壊さない。
- `AnswerQuestionInput`は`starting_agent`契約の一部として削除し、`AnswerQuestionResult`は
  `RunResult.final_output`の型として維持する(削除前に他consumerの不在を確認する)。
- `build_answering_runner()`はRunnerとlazy phases factoryの配線だけを行い、graph構築を
  `run()`の前へ前倒ししない。workerは同じreporter群(progress / events / delta / continuation)と
  session_factoryを`build_answering_runner()`へ渡し、`runner.run(input, run_context=...,
  hooks=...)`を1回だけ呼ぶ契約を維持する。
- `scripts/probe_question_answering.py`をOrchestrator / `AnswerQuestionInput`直接利用から
  Runnerのport注入形へ移行し、consumer inventoryに含める。
- API / DB / Redis event / Taskiq message / dependencyを変更しない。

### Non-goals

- Direct / Evidence AnswerのAgent宣言化とstreaming runtime(PR7)。
- retrieval dispatchと`ExternalResearchRuntimeFactory` activationのRunner移動(PR8)。
- external pipelineの展開と`ExternalSearchResearchRunner`削除(PR9)。
- 新しいphase spanの追加、progress語彙・event順序の変更。
- `QuestionPlanningService`等のphase policyをRunnerへ畳むこと(PR10で再訪)。
- prompt、model、retry、timeout、evidence collection内部の変更。

### Done

- `AnsweringRunner.run()`からcontext preparation、planning、direct / retrieval分岐、
  answer phase、result assembly、`RunResult`構築の順序を追える。
- `QuestionAnsweringOrchestrator` / `QuestionAnsweringAgent` / `starting_agent`が存在せず、
  Agent語彙が個別のLLM役割宣言だけを指す。
- 同一入力に対する`AnswerQuestionResult`、progress順序、error伝播、span記録が移行前と同値である。
- worker / composition / regression testが新しい構築・呼び出し形だけを参照する。

## Test contract

実装は挙動固定を先行させる: 以下のうちtimeline・短絡・assembly同値のテストを
orchestrator構成のまま先に書いてgreenにし、そのseatbeltの下でworkflowを移す。

- prepare・hook・phases factory・progress・各port呼び出しを単一のtimeline recorderへ記録し、
  branch別の全順序を固定する。direct pathは
  `prepare -> hook -> phases factory -> progress(planning) -> planner -> progress(synthesizing) ->
  direct answerer`、retrieval pathは
  `prepare -> hook -> phases factory -> progress(planning) -> planner -> progress(retrieving) ->
  collector -> progress(synthesizing) -> evidence answerer`の順である
  (progress同士の並びだけでなく、phase実行との相対順序を含めて検証する)。
- 非選択のportが0回である(direct pathでcollector / evidence answerer 0回、retrieval pathで
  direct answerer 0回)。
- context preparation失敗・hook失敗の各経路で、phases factoryを含む後続の呼び出しが0回である。
- planning / collect / answerの各段の失敗時、後続のprogressとportが起動しない。
- phases factoryの構築失敗が、hook発火済みかつ`agent_answering_run` span内で現行の遅延構築と
  同じ位置・分類で観測される。
- 同一の`QuestionContext` instance、`as_of`、`previous_answer`、`target_time_window`が
  各portへ伝播する。
- 未知citation ref・未知requirement idが既存の`EvidenceAnswerDraftInvalidError`になる。
- missing集計・dedupe・status導出・answered時のmissing空化が既存と同値である
  (代表入力のsnapshot比較)。
- direct / internal / external / mixedの各pathで`AnswerQuestionResult`が移行前と同値である。
- `AnswerGenerationStopped`が同一instanceでworkerへ届き、run spanがerrorにならない。
- 未分類例外が変換されずworker境界まで伝播する。
- `run()`が`starting_agent`を受け取らず、workerが`build_answering_runner()`と`run()`を
  各1回だけ呼ぶ。
- `agent_answering_run` spanのattributeが`run_id`だけである(新attribute / 新spanが増えていない)。
- result assemblyの純関数群が`answering/result_assembly.py`にあり、`running/`配下に
  ドメイン規則が存在しない。
- 旧orchestrator / starting agent系のsymbolが定義・package re-exportの両方から残存しない。
- `scripts/probe_question_answering.py`がRunnerのport注入形で動き、旧Orchestrator /
  `AnswerQuestionInput`への参照を残さない。

Exit gate: 親仕様`FLOW-01`〜`FLOW-05`、`FLOW-07` / `FLOW-08`、`RUN-03`〜`RUN-07`、
`ARCH-02` / `ARCH-03`。

残すseam: `EvidenceCollectionService`(dispatch owner)、`ExternalSearchService`(factory activation
owner)、`DirectAnswerFlow` / `EvidenceAnswerFlow`(PR7まで)。
削除するseam: `QuestionAnsweringOrchestrator`、`QuestionAnsweringAgent`、`starting_agent`、
`_DeferredQuestionAnsweringAgent`、`build_question_answering_agent()`、
`app/agent/__init__.py` / `answering/__init__.py`の旧symbol re-export、probeの旧Orchestrator直接利用。
