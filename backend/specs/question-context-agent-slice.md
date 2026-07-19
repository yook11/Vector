# Question Context Agent slice 仕様

更新日: 2026-07-19

実装状況: Implemented

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md`のPR6を具体化する。

question context工程(生の質問と bounded history から`QuestionContext`を準備する回答Runの最初の工程)を、
PR1で確立した共通`Agent`宣言と`GeminiAgentRuntime`へ移す。context preparationのpolicy——1回性、
履歴投影、safe fallback、finalize、telemetry——は`QuestionContextService`に残し、動かさない。

前提: PR1(共通Agent / Runtime / scope factory)。`context_preparer` portの位置はPR5でも不変のため、
本sliceはPR5と独立に実装できる(計画順はPR5後)。

## Work Definition

### Problem

- `GeminiQuestionContextGenerator`が役割宣言、prompt構築、client、API呼び出し、parseをまとめて持ち、
  plannerで解体済みの「全部入りadapter」構造が残っている。
- model / schema / prompt versionが`GeminiQuestionContextSpec`へ集約され、共通`Agent`宣言から
  役割と入出力契約を読み取れない。
- defect codeが`question_resolution_*`という現行名(question_context)より古い語彙のまま残っている。
- 失敗の観測がlogの`failure_type=例外class名`に依存し、audit(DB)は無く、`prompt_version`の
  記録消費者が存在しない。

### Evidence

- `question_context/service.py::prepare()`は`generate()`を1回だけ呼び、分類済み失敗
  (`AIProviderError` / `QuestionContextResponseInvalidError` / `ValidationError`)で即fallbackする。
  attempt loopも`previous_error`もworkflow timeoutも存在しない。
- fallbackは生の質問から決定的にcontextを構築し、回答Runを止めない。`failure_type`には
  例外class名(未設定時は`generator_unavailable`)がlogへ記録される。
- `generator=None`(Gemini未設定)はcompositionがbuild時に判定して注入し、その場合はLLM呼び出しも
  client生成も行われない。
- 設定済みの場合、clientは`GeminiQuestionContextGenerator.__init__`で生まれる。すなわち
  `build_answering_runner()`時点=`agent_answering_run` spanの外である。
- 履歴投影はserviceが所有する: message本文2000字cap、assistantの`missing_aspects`の
  正規化・重複排除・8件/300字cap。履歴6件の上限は`HISTORY_MESSAGE_LIMIT`をworkerの
  bounded history query(`queue/tasks/agent_run.py`)が適用する。
- 履歴が空のrunでもLLMは呼ばれ、serviceが`standalone_question`を生の質問で上書きし、
  `relevant_prior_coverage`を空にする決定的補正を行う。
- `gemini_spec.py`: model `gemini-2.5-flash-lite`、temperature 0.1、max_output_tokens 1024、
  `response_mime_type=application/json`、手書きGemini schema、`system_instruction=None`、
  旧`compute_call_signature()`によるversion、休眠rate limit policy(rpd 1500 / rpm 100)。
- `ai/prompts.py`のtemplateは固定ヘッダ -> untrustedな質問 / 履歴 -> 固定Rulesの配置で、
  質問・履歴本文・missing aspectsは`sanitize_for_untrusted_block()`と`<untrusted_input>`境界で
  投影される。
- metricsは`record_question_context_outcome(result, explicit_feedback_detected,
  previous_answer_had_missing_aspects)`の3属性。draft分割(`QuestionContextDraft`と
  `question_context_from_draft()`)は既に存在する。
- 現行serviceの`ValidationError`捕捉には2つの発生源がある: LLM draftのvalidation(旧adapter内)と、
  service自身が呼ぶfinalize(`question_context_from_draft()`)のvalidationである。後者は
  空白のみの`standalone_question`等、draftとしては受理されるが完成`QuestionContext`としては
  違反する値で発生し、例外にdraft由来の値が含まれる。
- 基底`AIProviderError.CODE`は値を持たないannotationであり、`exc.CODE`の単純参照は
  具象classがCODEを定義しない場合に`AttributeError`になる。
- `ai/__init__.py`は削除対象のadapterを無条件importしており、adapterだけを消すと保持対象の
  `schema_tool`のimportも壊れる。

### Invariants

#### Agent宣言とtyped input

- Question Context Agentをstable name `question_context`で、共通`Agent[InputT, OutputT]`の
  instanceとして宣言する。`AgentPrompt`(手動version / 固定instructions / 同期input renderer)、
  `ModelTarget` / `ModelSettings`(現行値)、`output_type=QuestionContextDraft`、
  手書き`response_schema`を関連づけ、rate limit policyを持たない。
- Prompt versionの正本は固定本文と同じ`question_context/prompts.py`に`"v1"`として置き、
  Agent組み立て側へliteralを複製しない。bump規則はplanner sliceの改訂版と同じ
  (instructions / input template / response schemaのmodel-visible変更で上げる)。
- 旧`compute_call_signature()`と旧hash値を持ち込まず、旧version値との連続性を要求しない。
- typed inputを次で固定する。

```python
@dataclass(frozen=True, slots=True)
class QuestionContextGenerationInput:
    question: str
    history: tuple[ThreadMessageSnapshot, ...]  # service投影済み
    as_of: datetime
```

- 履歴投影(2000字cap、missing aspectsの正規化・重複排除・8件/300字)は入力構築としてserviceが
  所有し続ける。input rendererは投影済み`InputT`だけから決定的にmodel-visible textを作り、
  既存の`sanitize_for_untrusted_block()`と`<untrusted_input>`境界を維持し、I/Oを行わない。
- 固定の役割・Rules・出力規則はすべてinstructionsへ集約し、Gemini requestでは
  `system_instruction`へinstructions、`contents`へrender済みtask inputを渡す。現行の
  単一contents構造からの変更は意図的に受け入れ、byte同一性を要求しない(PR2と同じ扱い)。
- `output_type`はPython側parse契約、`response_schema`はmodel向けwire契約であり、自動生成しない。
  整合性はcontract testで守る。finalize(`question_context_from_draft()`、無履歴時の
  `standalone_question`上書きと`relevant_prior_coverage`空化)はserviceが所有する。

#### 実行とresource scope

- `QuestionContextService`は`generator`の代わりに`agent`と
  `runtime_scope_factory: AgentRuntimeScopeFactory | None`を受け取る。
- Gemini未設定はcompositionがbuild時に判定し、factoryの代わりに`None`を注入する
  (未設定は実行時のイベントではなく配線時に解決する状態として表現する)。`None`の場合は
  現行どおりLLM / client / spanを一切作らず、`generator_unavailable`でfallbackする。
  activate時に設定例外を投げる方式は採らない。
- 設定済みの場合、`prepare()` 1回につきscopeを1回activateし、`invoke(agent, input,
  attempt_number=1)`を1回だけ呼ぶ。clientの誕生はbuild時からphase scope内へ移る。
  これは意図的な挙動変更として受け入れる(未設定経路の挙動は変えない)。
- scope factoryは`activate_planner_runtime`を汎用名(`activate_gemini_agent_runtime`)へ改名して
  共用する。共用するのは関数定義だけであり、activateごとに新しいclientを生成する。
  context phaseとplanning phaseはそれぞれ独立のscope / clientを持ち、回答Run全体での
  Gemini client共有は親仕様「PR7以降」の専用sliceで扱う。planner slice仕様の関数名記述を
  あわせて更新する。
- retryを導入しない: attemptは常に1、`previous_error` / 修正promptの経路を新設せず、
  workflow timeoutも追加しない(現行固定)。

#### Error、fallback、観測

- envelope違反はruntime所有の共通`AgentResponseInvalidError`(3 defect)へ統一し、
  `QuestionContextResponseInvalidError`と`question_resolution_*` defectを削除する。
  LLM出力のvalidation由来の`ValidationError`はruntime境界の外へ生で出ない。
- serviceはruntime境界の`AIProviderError` / `AgentResponseInvalidError`と、service自身のfinalize
  (`question_context_from_draft()`と無履歴補正)から発生する`ValidationError`を別のcatch境界で
  fallbackへ写像する。runtimeから生の`ValidationError`が出た場合はruntime契約違反として伝播させ、
  `context_finalize_invalid`へ誤分類しない。捕捉した例外の
  message / 値(draft本文を含みうる)をlog / metric / traceの語彙へ使わない。
  fallback contextの構築・telemetry・「回答Runを止めない」性質を現行同値に保つ。
- 失敗の記録語彙を例外class名依存から安定語彙へ意図的に変更する:
  `failure_code = generator_unavailable | 既知provider errorのcode | AgentResponseDefect値 |
  context_finalize_invalid`(finalize由来のValidationError)。logの`failure_type`も同じ語彙にする。
- `failure_code`への写像はtotalにする: `AIProviderError`系は具象classの`CODE`があればその値、
  無ければ固定値`provider_error`へ落とす。`exc.CODE`の単純参照(`AttributeError`でfallback処理
  自体が壊れる形)を実装しない。
- audit(DB記録)は導入せず、metricsへ統一する。`record_question_context_outcome`へ
  `prompt_version`(=`agent.prompt.version`)、`ai_model`(=`agent.model.name`)、失敗時の
  `failure_code`をlow-cardinality属性として追加する。これ以外の属性は増やさない。
- phase spanはfactoryが注入されている場合だけ`agent_phase`をserviceが1本開く。
  独自attribute allowlistは`phase="question_context"`と`agent_name="question_context"`の
  2つとする。fallbackで完了してもphase spanをerrorにしない(失敗の可視化はattempt spanの
  `result` / `error.type`とmetricsが担う)。
- attempt span(`agent_provider_call`)は`GeminiAgentRuntime`の既存契約をそのまま使い、
  本sliceで観測実装を追加・変更しない。
- prompt本文とprompt versionを区別して扱う: question・履歴本文・instructions・render済みinput・
  draftなどmodel-visibleな**本文**はspan / metric / logへ一切載せない。一方prompt **version**は
  low-cardinalityなmetadataであり、本sliceのmetric属性へ記録する。attempt spanへのversion記録の
  有無はruntime共通契約に従い、本sliceで変更しない。

### Non-goals

- retry、修正prompt、workflow timeoutの導入。
- 回答Run全体のGemini client共有(親仕様「PR7以降」の専用slice)。
- workerのbounded history取得(`HISTORY_MESSAGE_LIMIT=6`)と履歴投影規則の変更。
- `QuestionContext`型、finalize規則、telemetry意味論の変更。
- Geminiを`ensure_question_answering_agent_configured()`へ加えてrun作成前503にすること
  (未設定degraded modeを廃止するかは別の製品判断として扱う)。
- prompt文面の意味変更(system / user分離とRules集約の再配置を除く)。
- API、DB、Redis event、Taskiq message、dependencyの変更。

### Done

- Question Context Agentが何者か(name / Prompt / model / 入出力契約)を1つの`Agent`宣言から
  読み取れ、旧adapter / call specの重複正本が消える。
- 未設定runでclient 0・LLM 0・`generator_unavailable`の挙動が維持される。
- 設定済みrunでclientがphase scope内に生まれ、attempt 1回・即fallback・Run継続の挙動が
  現行同値である。
- metricsが`prompt_version` / `ai_model` / `failure_code`で層別でき、audit(DB)への新規記録が無い。
- phase / attempt spanが同じ回答trace配下に観測でき、model-visible textが漏れない。
- 既存question context regressionとCTX gateが通る。

## 責任境界

| 責任 | Agent宣言 | GeminiAgentRuntime(既存) | QuestionContextService | composition |
|---|:---:|:---:|:---:|:---:|
| 役割・Prompt・version・model・schemaの正本 | ○ | - | - | - |
| 1 attempt実行・attempt span・usage | - | ○(実装追加なし) | - | - |
| 履歴投影(typed input構築) | - | - | ○ | - |
| 1回性・fallback・finalize・telemetry | - | - | ○ | - |
| metrics(prompt_version / failure_code含む) | - | - | ○ | - |
| phase span(factory注入時のみ) | - | - | ○ | - |
| 設定判定とfactory / None注入 | - | - | - | ○ |
| 共用scope factory実装 | - | - | - | ○ |

配置: Agent宣言は`question_context/agent.py`、固定本文・version・rendererは
`question_context/prompts.py`(現`ai/prompts.py`を再編)、typed inputは`question_context/contract.py`。
`ai/gemini.py`、`ai/gemini_spec.py`、`ai/gemini_prompt.py`は削除し、`ai/schema_tool.py`は
schema正本として維持する。`ai/__init__.py`の無条件adapter importを削除対象へ含め、
schema_toolのimportが単体で成立する形にする。

削除・rename inventory(定義とpackage re-exportの両方を対象とする):

- 削除: `GeminiQuestionContextGenerator`、`GeminiQuestionContextSpec`、
  `GeminiQuestionContextPrompt`、`QuestionContextResponseInvalidError`、
  `question_resolution_*` defect、`QuestionContextGenerator` protocol、
  `build_question_context_generator()`、`ai/__init__.py`の旧export。
- rename: `activate_planner_runtime` -> `activate_gemini_agent_runtime`(旧名のaliasを残さない。
  planner側の配線とplanner slice仕様の記述を同時に更新する)。

## Test contract

- Agent宣言がfrozenで、stable name `question_context`、`AgentPrompt`、`ModelTarget` /
  `ModelSettings`(現行値)、`output_type`、`response_schema`を持ち、rate limit policyを持たない。
- Prompt versionが`prompts.py`で固定本文と隣接し、`agent.py`へliteralを複製しない。
- input rendererが`QuestionContextGenerationInput`だけから決定的にrenderし、sanitize /
  `<untrusted_input>`境界を維持し、I/Oを行わない。
- 履歴投影(2000字cap、missing aspects正規化・重複排除・8件/300字)がservice所有のまま同値である。
- workerのbounded history上限6件が不変である(参照のみ)。
- 未設定(factory None): client構築0回・invoke 0回・phase span 0本で、`generator_unavailable`の
  log / metricとfallback contextが現行同値である。
- 設定済み成功: scope activateが`prepare()` 1回につき1回、invokeが1回(`attempt_number=1`)、
  clientの誕生がactivate後である。無履歴時の上書き補正とtelemetryが現行同値である。
- 分類済み失敗(provider error / blocked / invalid responseの各経路)で追加attemptが無く、
  即fallbackし、回答Runが継続する。LLM出力validation由来の`ValidationError`が生で
  service境界へ届かない。
- finalize(`question_context_from_draft()`)が`ValidationError`になる入力(空白のみの
  `standalone_question`等)でもfallbackし、回答Runが継続する。`failure_code`は
  `context_finalize_invalid`で、例外messageのdraft本文がlog / metric / traceへ現れない。
- `CODE`を定義しない`AIProviderError`でも`AttributeError`にならず、`failure_code`が
  固定値`provider_error`へ落ちてfallbackが完走する。
- `failure_code`が安定語彙(generator_unavailable / provider code / provider_error / defect値 /
  context_finalize_invalid)であり、例外class名がlog / metricの語彙に現れない。
- metricsに`prompt_version` / `ai_model` / 失敗時`failure_code`が追加され、それ以外の属性が
  増えていない。
- phase spanのallowlistが`{phase, agent_name}`の2つで、fallback完了時にerrorにならない。
- phase span / attempt spanが`agent_answering_run`の子として同一traceに属する(親子関係を検査する)。
- attempt spanで既存runtime契約(gen_ai属性 / result / usage)がこの工程でも成立する。
- scope enter(activate)の失敗が分類済みfailureとしてfallbackへ落ち、client / spanが残らない。
- 成功・分類済みfailure・finalize失敗・未分類例外・cancellationの各経路で、fake clientの
  closeがちょうど1回である(共用factoryのclose契約がこの呼び出し箇所でも成立する)。
- fake Gemini clientによるrequest-level test: `system_instruction`が固定instructionsのみ、
  `contents`がrender済みtask inputのみで、question / 履歴 / missing aspectsのsentinelが
  `system_instruction`側に現れず、固定防御文が`contents`側に現れない。
- response schemaと`QuestionContextDraft`の整合性test(field / required / enum / 代表payload)。
- 旧symbol(`GeminiQuestionContextGenerator`、`GeminiQuestionContextSpec`、
  `GeminiQuestionContextPrompt`、`QuestionContextResponseInvalidError`、`question_resolution_*`、
  `QuestionContextGenerator` protocol、`build_question_context_generator()`、
  `activate_planner_runtime`旧名、`model_name` / `prompt_version` property)が定義・
  package re-exportの両方から残存しない。`ai/__init__.py`経由の`schema_tool` importが
  adapter削除後も成立する。
- golden一読: 履歴あり / なしそれぞれの`system_instruction`と`contents`を人間が確認する
  手順を実装PRに含める(request-level testの補助として)。
- 既存question context preparation regressionが通る。

Exit gate: 親仕様`CTX-01`〜`CTX-04`。

## 実装時のgolden確認

2026-07-19に、固定`system_instruction`とrender済み`contents`を履歴あり・なしの2経路で
人間が一読した。

- 履歴なし: `system_instruction`は固定instructionsだけ、`contents`は`as_of`・現在の質問と
  空のPrior Thread Messagesだけで構成されている。
- 履歴あり: `system_instruction`へ質問・履歴・`missing_aspects`が混入せず、`contents`側だけに
  service投影済みの各値が`<untrusted_input>`境界内で現れる。
- 両経路とも固定Rulesは`system_instruction`だけにあり、`contents`へ複製されていない。

残すseam: `ai/schema_tool.py`(schema正本)、workerのbounded history取得、`QuestionContext`型と
finalize規則。
削除するseam: 旧adapter / call spec / prompt renderer module、`question_resolution_*`語彙、
休眠rate limit policy(値ごと削除し、適用するsliceで再定義する)。
