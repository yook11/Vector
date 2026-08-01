# Agent phase span の所属工程を実工程語彙へ揃える slice 仕様

更新日: 2026-08-01

実装状況: Implemented — 2026-08-01

## 位置付け

`agent-progress-stage-vocabulary-slice.md`(#88)がstage語彙を実工程6値へ、
`agent-activity-event-vocabulary-slice.md`(#99)がactivityイベント名を工程プレフィクスへ
移した。本sliceは最後の1本として、Logfire spanの`phase`属性を同じ工程語彙へ揃える。

先行2 sliceがNon-goalsで「phase spanの`phase`属性を工程名へ揃えること(PR3)」と繰り返し
境界を切ってきた。本sliceがその続きにあたる。

観測系のうちmetric名は対象外である。工程単位ではなくAgent・機構単位の語彙であり、名前変更が
時系列の断絶を伴うためで、この判断は先行sliceから変えない。

## Work Definition

### Problem

- `phase`属性の値が工程を指していない。`question_planning`はAgent名寄り、`external_query`は
  機構名、`evidence_answer` / `direct_answer`はAgent名である。工程名と一致するのは
  `evidence_review`だけで、6工程のうち1つしか揃っていない。
- `agent_phase()`ヘルパーが`app/agent/phase_span.py`にあるのに、4箇所がこれを使わず
  `_PHASE_SPAN_NAME = "agent_phase"`を各自で再宣言して`logfire.span`を直接呼んでいる。
  span名も属性の並びも規約ではなく慣習で保たれている。
- 同じ工程spanなのに、終了をどう分類するかが呼び出し箇所ごとに違う。停止制御
  (`AnswerGenerationStopped`)をerrorにしないのはanswering 2箇所だけ、分類済み失敗を
  素通しするのはplanningだけ、question_contextは終了分類を何も持たない。同じ概念の実装が
  3通りに分かれており、どれが規約なのかコードから読めない。
- `safety_check`工程にspanが無い。metricで成功・失敗の件数は出るが、traceには現れない。
- `evidence_collection`は根拠を集める工程だが、spanがあるのは外部クエリ生成だけである。
  内部検索(embedding生成 + ベクトル検索)はspanがゼロで、metricの成功・失敗件数しか残らない。
  外部検索がAgent spanとTavilyのtool spanの2層で観測されているのに対し、内部検索だけが
  trace上で空白になっている。同じ工程の2機構で観測の厚みが違う理由がない。

### Evidence

以下は着手時点(`origin/main`、#99 merge 直後)の状態であり、行番号もその時点のもの。

span 構成:

| # | 工程 | spanの作り方 | `phase`値 | 粒度 |
|---|---|---|---|---|
| 1 | safety_check | **なし** | — | — |
| 2 | context_resolution | `logfire.span`直接(`question_context/service.py:197`) | `question_context` | Run 1回 |
| 3 | planning | `logfire.span`直接(`planning/service.py:123`) | `question_planning` | Run 1回 |
| 4 | evidence_collection (外部クエリ生成) | `agent_phase()`(`researcher.py:169`) | `external_query` | taskごと |
| 4 | evidence_collection (内部検索) | **なし** | — | — |
| 5 | evidence_review | `agent_phase()`(`evidence_review/reviewer.py:62`) | `evidence_review` | Run 1回 |
| 6 | answering (evidence) | `logfire.span`直接(`evidence_answer/flow.py:216`) | `evidence_answer` | Run 1回 |
| 6 | answering (direct) | `logfire.span`直接(`direct_answer/flow.py:173`) | `direct_answer` | Run 1回 |

- `agent_phase()`は`phase` / `agent_name`を必須、`task_index`を任意で受け、
  `logfire.span("agent_phase", ...)`を作る。例外時に`StatusCode.ERROR`を立てて再送出する。
- span名`agent_phase`の文字列は5箇所(ヘルパー + 直接呼び出し4箇所)に重複している。
- 終了分類の現状は4通りに分かれている。`agent_phase()`は全例外をERRORにする。planningは
  分類済み失敗(`AIProviderStateError` / `AIProviderContentError` /
  `AgentResponseInvalidError`)を素通しし未分類だけdescriptionを付ける。question_contextは
  status操作を持たない。answering 2箇所は`AnswerGenerationStopped`をspan内で握って
  span終了後に再送出する。
- 子spanの`agent_provider_call`は既に終了分類を持つ。分類済み失敗は
  `_record_classified_error()`が`result` / `error.type`属性を置き
  `set_status(StatusCode.ERROR)`をdescriptionなしで呼び、例外はspanを閉じてから再送出する
  (`gemini.py:435-443` / `:325-330`)。`record_exception`を呼ぶのは未分類経路だけである
  (`gemini.py:319-320`)。工程spanだけがこの規律から外れている。
- `error.type`の値はsemconvの`ERROR_TYPE`で、`AgentResponseInvalidError`は
  `exc.defect.value`、provider errorは`error.CODE`(無ければクラス名)を使う。
- 分類済み失敗のすべてが`agent_provider_call`を通るわけではない。planningの
  `plan_from_draft()`は`runtime.invoke()`が返った後に
  `AgentResponseInvalidError(OUTPUT_SCHEMA_MISMATCH)`を上げる
  (`planning/service.py:82` → `planning/contract.py:355`)。この経路ではattempt spanが
  `result="succeeded"`で閉じており、工程spanが唯一の記録になる。
- `app/logfire/redaction.py`はexport直前に非空の`status.description`と例外の自由文属性を
  `[redacted]`へ置換する。`exception.type`と`status_code`は残る。descriptionの文言差は
  production では観測できない。
- 例外がspanを貫通するとき、logfireは`BaseException`を含むすべての例外について
  status ERROR・description・exception eventを自動で付ける。貫通前に呼ぶ
  `span.set_status(StatusCode.ERROR, "unclassified agent phase error")`はこの自動記録に
  必ず上書きされるため、4箇所すべてで無効である(`asyncio.CancelledError`でも実測で確認)。
  現状の「未分類例外にdescriptionを付ける」実装は効果を持っていない。
- 分類済み失敗の語彙は畳める。`AIProviderStateError`と`AIProviderContentError`は
  `AIProviderError`のsubclassであり、planningの`_PLANNER_CLASSIFIED_ERRORS`は
  question_contextの`_RUNTIME_FAILURES`(`AIProviderError` / `AgentResponseInvalidError`)の
  部分集合である。
- `researcher._collect_external()`と`EvidenceReviewer.review()`は分類済み失敗と
  `TimeoutError`をspan内で握って縮退結果へ変える。例外がspanを抜けないため、終了分類を
  入れても両者の挙動は変わらない。
- `_answering_run_span`(`answering_runner.py:561-570`)は
  `(AnswerGenerationStopped, InputSafetyBlocked)`を握ってspan終了後に再送出する同じ
  イディオムを持つ。run spanの語彙は工程spanとは別に決める必要がある。
- 内部検索の観測は`internal_search/metrics.py`のcounter 2本
  (`vector.agent.internal_retrieval.outcome` / `.query_embedding_cache`)のみである。
  embedding生成(Gemini呼び出し)にもspanは無い。
- 外部検索のTavily呼び出しは`external_search/tavily.py:78`で
  `logfire.span(_TOOL_SPAN_NAME, _span_kind=CLIENT, tool_name=...)`を持つ。これは
  `agent_phase`とは別のspanである。
- `InternalSearchError(phase=...)`の`phase`は`article_search` / `query_embedding`という
  失敗箇所の識別子であり、工程名とは別概念である。同じ語が2つの意味で使われている。
- spanの階層は`agent_answering_run` → `agent_phase` → `agent_provider_call`。

### Invariants

#### phaseはそのspanがどの工程に属するかを表す

- 値はstage語彙と同じ6値(`safety_check` / `context_resolution` / `planning` /
  `evidence_collection` / `evidence_review` / `answering`)とする。
- 工程の区間そのものを表すspanではない。1つの工程から複数のspanが出てよい(taskごと、
  経路ごと)。工程全体を包む親spanは作らない。
- direct answerとevidence answerは同じ`phase`になる。経路の区別は`agent_name`が担う。

#### spanの生成は`agent_phase()`に集約する

- `logfire.span`を直接呼ぶ4箇所をヘルパー経由へ移す。
- `_PHASE_SPAN_NAME`の重複宣言を消し、span名の定義を1箇所にする。
- 呼び出し箇所のlocal contextmanagerと`_record_unclassified_phase_error()`を消す。

#### 終了分類はヘルパーが持ち、`agent_provider_call`と同型にする

工程spanの終了を次の4分類にする。呼び出し側に例外の一覧を渡させず、ヘルパーが共有契約の
例外型を自分で認識する。

| 終了分類 | status | exception event | 属性 | 再送出 |
|---|---|---|---|---|
| 正常終了 | UNSET | なし | — | — |
| 意図された停止 (`AnswerGenerationStopped`) | UNSET | なし | — | span終了後に同一インスタンス |
| 分類済み失敗 (`AIProviderError` / `AgentResponseInvalidError`) | ERROR (description なし) | なし | `error.type` | span終了後に同一インスタンス |
| 未分類例外 | ERROR (logfire既定) | あり | — | spanを貫通 |

- 停止の語彙は`app/agent/contract.py`の`AnswerGenerationStopped`、分類済み失敗の語彙は
  `(AIProviderError, AgentResponseInvalidError)`とする。既存3箇所の分類はこの2つに畳める。
- 分類済み失敗の`error.type`は`agent_provider_call`と同じ値を使う
  (`AgentResponseInvalidError`は`defect.value`、それ以外は`error.CODE`)。attempt spanを
  通らない分類済み失敗でも、種別がtraceに残ることを保証するために置く。
- 未分類例外に対してヘルパーは何もしない。spanを貫通させ、logfireの自動例外記録に任せる。
  status・description・exception eventはlogfireが付け、自由文はexport境界でredactされる。

#### 停止と分類済み失敗は呼び出し側から見て透過である

- ヘルパーが握るのはspanの終了分類のためだけであり、同一の例外インスタンスをspan終了後に
  再送出する。呼び出し側の制御フローとexceptionの同一性を変えない。

#### `agent_name`はAgentが実行するspanにだけ付ける

- `agent_name`を任意引数にする。内部検索のようにAgentでない機構が実行するspanでは省略する。
- Agent名の名前空間にAgentでないものを入れない。工程名とAgent名を別軸として扱う先行slice
  の判断を守る。

#### 観測が欠けている工程を埋める

- `safety_check`にspanを足す。Run単位1回。
- `evidence_collection`の内部検索にspanを足す。taskごとで、外部クエリ生成と同じ粒度にする。
- 追加するのは工程の所属が読めるspanまでとする。工程の内訳(embedding生成とベクトル検索の
  切り分け)は足さない。
- 内部検索の`InternalSearchError`はtoolの失敗であり、Agent runtimeの共有失敗契約
  (`AIProviderError` / `AgentResponseInvalidError`)ではない。工程spanの終了分類では未分類
  として貫通させ、logfireの自動例外記録に任せる。researcherがspanの外で握って縮退へ変える
  ため、呼び出し側に例外は伝わらない。

#### 既存の観測機構を壊さない

- span名`agent_phase`を変えない。
- metric名を変えない。
- Tavilyのtool span、`agent_provider_call`、`agent_answering_run`を変えない。
- `task_index`属性の意味を変えない。
- 分類済み失敗・停止・未分類例外のいずれでも、呼び出し側が受け取る例外を変えない。

### Non-goals

- metric名の変更。`vector.agent.answer_synthesis.outcome` /
  `vector.agent.internal_retrieval.outcome` / `vector.agent.planner.outcome`はAgent・機構
  単位の語彙であり、名前変更は時系列の断絶を伴う。
- Agent宣言名の変更。
- embedding生成とベクトル検索を分ける内訳span。工程レベルの可視化が先で、内訳が要るかは
  実測してから判断する。
- `agent_provider_call` spanが出す属性の値と`gen_ai.*`の意味論。`error.type`の値を決める
  写像は工程spanと共有するが、provider spanが記録する値そのものは変えない。
- 新しいmetricの追加。工程spanへの計測値の追加(所要時間、件数など)。`error.type`は終了分類を
  成立させるために置くので対象外とする。
- `agent_answering_run` spanの終了分類。停止語彙に`InputSafetyBlocked`を含み、これは工程の
  停止ではなくrunの終端事由である。同じ4分類を当てるかはrun層で別に決める。本sliceは
  工程spanとヘルパーに閉じる。
- `result`属性を工程spanへ置くこと。`agent_provider_call`は持つが、工程単位のresult語彙を
  新しく決める必要があり本sliceの範囲を超える。
- `InternalSearchError(phase=...)`の語彙。失敗箇所の識別子であり工程名とは別概念である。
  同じ語が2つの意味を持つ問題は残るが、失敗分類の再設計として別に扱う。
- 工程全体を包む親spanの新設。

### Done

- 6工程それぞれのspanが`phase`属性を持ち、値がstage語彙と一致している。
- `agent_phase()`がspan生成の唯一の入口であり、`_PHASE_SPAN_NAME`の重複宣言が消えている。
- `agent_name`が任意引数になり、Agentが実行するspanにだけ付いている。
- `safety_check`のspanがtraceに現れる。
- 内部検索のspanがtaskごとにtraceに現れ、`evidence_collection`工程に属することが読める。
- span名`agent_phase`、metric名、他のspan(`agent_provider_call` / Tavily tool span /
  `agent_answering_run`)が変わっていない。
- 終了分類の4値がヘルパー1箇所で実装され、呼び出し側にlocalな終了分類が残っていない。
- 停止(`AnswerGenerationStopped`)でspanがERRORにならず、同じ例外が呼び出し側へ届く。
- 分類済み失敗でspanがERRORになり`error.type`が付き、exception eventが付いていない。
- 未分類例外でspanがERRORになり、exception eventが付き、同じ例外が貫通する。

## 工程とspanの対応

| # | 工程 | `phase` | spanを作る場所 | `agent_name` | 粒度 |
|---|---|---|---|---|---|
| 1 | safety_check | `safety_check` | `input_safety/service.py`(新規) | `input_safety` | Run 1回 |
| 2 | context_resolution | `context_resolution` | `question_context/service.py` | `question_context` | Run 1回 |
| 3 | planning | `planning` | `planning/service.py` | `question_planner` | Run 1回 |
| 4 | evidence_collection | `evidence_collection` | `researcher.py`(内部検索、新規) | **なし** | taskごと |
| 4 | evidence_collection | `evidence_collection` | `researcher.py`(外部クエリ生成) | `external_query_generator` | taskごと |
| 5 | evidence_review | `evidence_review` | `evidence_review/reviewer.py` | `evidence_reviewer` | Run 1回 |
| 6 | answering | `answering` | `evidence_answer/flow.py` | `evidence_answer` | Run 1回 |
| 6 | answering | `answering` | `direct_answer/flow.py` | `direct_answer` | Run 1回 |

`evidence_collection`と`answering`は同じ`phase`のspanが複数出る。前者はtask並列と2機構、
後者は経路の分岐による。`agent_name`と`task_index`で切り分けられる。

## 影響範囲

### backend

- `app/agent/phase_span.py` — `AgentPhase`語彙、`agent_name`任意引数、終了分類4値
- `app/agent/error_type.py` — 新規。`error.type`が名乗る失敗種別の唯一の写像
  `span_error_type()`。工程spanとprovider spanが同じ語彙を使うことを、テストではなく
  実装が1つであることで保証する
- `app/agent/runtime/gemini.py` / `app/agent/runtime/deepseek.py` — 各自が持っていた
  `_provider_error_type()`と`exc.defect.value`の直書きを`span_error_type()`へ寄せる。
  span属性の値は変わらない
- `app/agent/input_safety/service.py` — span新規(`check()`全体を包む)
- `app/agent/question_context/service.py` — local contextmanagerを消しヘルパー経由へ、`phase`値
- `app/agent/planning/service.py` — 同上。`_PLANNER_CLASSIFIED_ERRORS`は分類済み失敗として
  ヘルパーが扱うため、span用の再送出分岐が不要になる(retry判定側の用途は残る)
- `app/agent/answering/evidence_answer/flow.py` — 同上。停止の握りはヘルパーへ移る
- `app/agent/answering/direct_answer/flow.py` — 同上
- `app/agent/evidence_collection/researcher.py` — `phase`値、内部検索のspan新規
- `app/agent/evidence_collection/evidence_review/reviewer.py` — `phase`値(定数のみ)

### frontend

変更なし。spanはfrontendに配信されない。

### テスト

新規4ファイル。`tests/agent/test_phase_span.py`(終了分類4値の正本) /
`tests/agent/test_phase_vocabulary_contract.py`(語彙の集合一致) /
`tests/agent/input_safety/test_service_tracing.py` /
`tests/agent/evidence_collection/test_researcher_tracing.py`。

既存5ファイルは`phase`値の追随。`tests/agent/planning/test_planner_tracing.py` /
`tests/agent/question_context/test_tracing.py` /
`tests/agent/answering/evidence_answer/test_flow.py` /
`tests/agent/answering/direct_answer/test_flow_tracing.py` /
`tests/agent/running/test_external_pipeline_tracing.py`。最後の1つは内部検索spanが
加わってphase spanが3本になるため、Agent所有と機構所有を分ける形へ直す。

## Test contract

### phase 属性

- 6工程それぞれのspanが期待する`phase`値を持つ。
- direct answerとevidence answerが同じ`phase`を持ち、`agent_name`で区別できる。
- 内部検索と外部クエリ生成が同じ`phase`を持ち、内部検索には`agent_name`が無い。

### span の生成

- span名が`agent_phase`である。
- `agent_name`を渡さないspanに`agent_name`属性が付かない(空文字やNoneで付けない)。
- `task_index`が負値のとき`ValueError`が上がり、spanが作られない。

### 終了分類

- 正常終了でspanがERRORにならず、`error.type`が付かない。
- `AnswerGenerationStopped`でspanがERRORにならず、exception eventが付かず、同一インスタンス
  が呼び出し側へ届く。
- `AIProviderError`と`AgentResponseInvalidError`でspanがERRORになり`error.type`が付き、
  exception eventが付かず、同一インスタンスが呼び出し側へ届く。`AgentResponseInvalidError`
  の`error.type`は`defect.value`である。
- 未分類例外でspanがERRORになり、exception eventが付き、同一インスタンスが貫通する。
- 停止と分類済み失敗を握るのはspan終了分類のためだけであり、呼び出し側の`except`が
  これまでどおり成立する(planningのretry、answeringの停止伝播、runnerの終端処理)。

### 新設した span

- `safety_check`のspanがRun 1回だけ作られ、ブロック時も失敗時も作られる。
- 内部検索のspanがtaskごとに作られ、`task_index`を持つ。
- 内部検索が失敗した(`InternalSearchError`)ときもspanが作られ、未分類としてERRORになり
  exception eventが付く(`error.type`は付かない)。`collect()`は例外を伝えず縮退する。
- 内部検索と外部クエリ生成の両方が`evidence_collection`の`phase`を持ち、`agent_name`の
  有無で区別できる。

### 壊していないこと

- `agent_provider_call` spanがこれまでどおり`agent_phase`の子として作られる。
- Tavilyのtool spanが変わらない。
- metricの記録がこれまでどおり行われる。

## 実装順

1 段で行う。frontendに影響が無く、API契約もDBも変わらないため分割の必要がない。

1. `agent_phase()`に`AgentPhase`語彙・任意の`agent_name`・終了分類4値を入れる。
2. `logfire.span`直接呼び出し4箇所をヘルパー経由へ移し、`phase`値を6語彙へ揃える。
   local contextmanagerと`_record_unclassified_phase_error()`を消す。
3. `safety_check`と内部検索のspanを新設する。

## 移行

DB変更なし、API契約変更なし、migration不要。frontendへの影響なし。

Logfireのクエリ・アラートで`phase`を条件にしているものは書き換えが必要になる。属性値の
変更なのでmetricの時系列は切れないが、deploy前後をまたぐ集計では新旧の値が混在する。
既存のダッシュボード定義がある場合は、deploy後に旧値のクエリを更新する。

分類済み失敗の工程spanからexception eventが消え、代わりに`error.type`が付く。工程spanの
exception eventを条件にしているクエリがある場合は`error.type`へ移す。ERRORステータスの
有無は変わらないため、error率のアラートは影響を受けない。

## 実装後に確認する運用値

- `safety_check`のspanの所要時間。stage表示のちらつき(先行sliceの残課題)を判断する材料になる。
- 内部検索と外部検索の所要時間の比。`evidence_collection`工程のどちらが待ち時間を占めるかが
  初めてtraceで見える。
- 内部検索の失敗がtraceに現れること。これまでmetricのcounterでしか分からなかった。
