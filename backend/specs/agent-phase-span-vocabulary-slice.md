# Agent phase span の所属工程を実工程語彙へ揃える slice 仕様

更新日: 2026-08-01

実装状況: Draft

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
- `safety_check`工程にspanが無い。metricで成功・失敗の件数は出るが、traceには現れない。
- `evidence_collection`は根拠を集める工程だが、spanがあるのは外部クエリ生成だけである。
  内部検索(embedding生成 + ベクトル検索)はspanがゼロで、metricの成功・失敗件数しか残らない。
  外部検索がAgent spanとTavilyのtool spanの2層で観測されているのに対し、内部検索だけが
  trace上で空白になっている。同じ工程の2機構で観測の厚みが違う理由がない。

### Evidence

現状の span 構成:

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
- 例外時にERRORステータスを立てる既存の挙動を変えない。

#### `agent_name`はAgentが実行するspanにだけ付ける

- `agent_name`を任意引数にする。内部検索のようにAgentでない機構が実行するspanでは省略する。
- Agent名の名前空間にAgentでないものを入れない。工程名とAgent名を別軸として扱う先行slice
  の判断を守る。

#### 観測が欠けている工程を埋める

- `safety_check`にspanを足す。Run単位1回。
- `evidence_collection`の内部検索にspanを足す。taskごとで、外部クエリ生成と同じ粒度にする。
- 追加するのは工程の所属が読めるspanまでとする。工程の内訳(embedding生成とベクトル検索の
  切り分け)は足さない。

#### 既存の観測機構を壊さない

- span名`agent_phase`を変えない。
- metric名を変えない。
- Tavilyのtool span、`agent_provider_call`、`agent_answering_run`を変えない。
- `task_index`属性の意味を変えない。

### Non-goals

- metric名の変更。`vector.agent.answer_synthesis.outcome` /
  `vector.agent.internal_retrieval.outcome` / `vector.agent.planner.outcome`はAgent・機構
  単位の語彙であり、名前変更は時系列の断絶を伴う。
- Agent宣言名の変更。
- embedding生成とベクトル検索を分ける内訳span。工程レベルの可視化が先で、内訳が要るかは
  実測してから判断する。
- `agent_provider_call` spanの属性、`gen_ai.*`の意味論。
- 新しいmetricの追加、spanへの新しい属性の追加(所要時間、件数など)。
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
- 例外時にERRORステータスが立つ既存の挙動が保たれている。

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

- `app/agent/phase_span.py` — `agent_name`を任意引数へ。属性はNoneのとき付けない
- `app/agent/input_safety/service.py` — span新規(`check()`全体を包む)
- `app/agent/question_context/service.py` — ヘルパー経由へ、`phase`値
- `app/agent/planning/service.py` — 同上
- `app/agent/answering/evidence_answer/flow.py` — 同上
- `app/agent/answering/direct_answer/flow.py` — 同上
- `app/agent/evidence_collection/researcher.py` — `phase`値、内部検索のspan新規
- `app/agent/evidence_collection/evidence_review/reviewer.py` — `phase`値(定数のみ)

### frontend

変更なし。spanはfrontendに配信されない。

### テスト

tracing系を中心に6ファイル前後。`tests/agent/planning/test_planner_tracing.py` /
`tests/agent/question_context/test_tracing.py` /
`tests/agent/answering/direct_answer/test_flow_tracing.py` /
`tests/agent/answering/evidence_answer/test_flow.py` /
`tests/agent/running/test_external_pipeline_tracing.py` /
`tests/agent/evidence_collection/test_researcher.py`。

## Test contract

### phase 属性

- 6工程それぞれのspanが期待する`phase`値を持つ。
- direct answerとevidence answerが同じ`phase`を持ち、`agent_name`で区別できる。
- 内部検索と外部クエリ生成が同じ`phase`を持ち、内部検索には`agent_name`が無い。

### span の生成

- span名が`agent_phase`である。
- `agent_name`を渡さないspanに`agent_name`属性が付かない(空文字やNoneで付けない)。
- 例外がspanを通過するとERRORステータスが立ち、同じ例外が再送出される。

### 新設した span

- `safety_check`のspanがRun 1回だけ作られ、ブロック時も失敗時も作られる。
- 内部検索のspanがtaskごとに作られ、`task_index`を持つ。
- 内部検索が失敗した(`InternalSearchError`)ときもspanが作られ、ERRORステータスが立つ。

### 壊していないこと

- `agent_provider_call` spanがこれまでどおり`agent_phase`の子として作られる。
- Tavilyのtool spanが変わらない。
- metricの記録がこれまでどおり行われる。

## 実装順

1 段で行う。frontendに影響が無く、API契約もDBも変わらないため分割の必要がない。

1. `agent_phase()`の`agent_name`を任意引数にし、`logfire.span`直接呼び出し4箇所を
   ヘルパー経由へ移す。同時に`phase`値を6語彙へ揃える。
2. `safety_check`と内部検索のspanを新設する。

## 移行

DB変更なし、API契約変更なし、migration不要。frontendへの影響なし。

Logfireのクエリ・アラートで`phase`を条件にしているものは書き換えが必要になる。属性値の
変更なのでmetricの時系列は切れないが、deploy前後をまたぐ集計では新旧の値が混在する。
既存のダッシュボード定義がある場合は、deploy後に旧値のクエリを更新する。

## 実装後に確認する運用値

- `safety_check`のspanの所要時間。stage表示のちらつき(先行sliceの残課題)を判断する材料になる。
- 内部検索と外部検索の所要時間の比。`evidence_collection`工程のどちらが待ち時間を占めるかが
  初めてtraceで見える。
- 内部検索の失敗がtraceに現れること。これまでmetricのcounterでしか分からなかった。
