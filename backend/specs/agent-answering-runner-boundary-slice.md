# Agent answering Runner 境界抽出 slice 仕様

更新日: 2026-07-20

実装状況: Implemented

> 後続設計: `agent-declaration-runner-orchestration-slice.md` は、このsliceで抽出したRunner境界を
> 維持しつつ、将来のworkflow ownerを`AnsweringRunner`へ移し、役割別AgentとToolを明示する。
> 本文のOrchestrator / ExternalSearchResearchRunner維持は、この実装slice内の移行制約として読む。
> Input safetyの最終配置は`agent-input-safety-gate-slice.md`をSSoTとし、本仕様§12の更新を適用する。

## 位置付け

現在の Research agent は、Postgres の run lifecycle、thread 履歴読み込み、
question context preparation、回答 agent の実行、結果永続化、Redis 通知を
`run_agent_answer()` がまとめて進行している。そのため、「1回のユーザー入力から
回答まで」と「永続 run の状態管理」の境界が、worker task から読み取りにくい。

本 slice は OpenAI Agents SDK の `Agent` / `Runner` 分離を参考にしつつ、Vector 内部に
次の実行境界を抽出する。

- `starting_agent`: 回答能力を実行する既存 `QuestionAnsweringAgent`。
- `Runner`: question context を準備し、starting agent を呼び、実行結果を返す。
- `AnsweringRunContext`: context preparation 完了後の、回答処理を開始できる
  run context。
- `RunResult`: final output とその出力を作った context を返す。

OpenAI Agents SDK そのものを導入する仕様ではない。Vector の現行ワークフローは
Python の決定的な plan dispatch を維持し、model が自由に tool や次の agent を選ぶ
agent loop へは変更しない。

前提仕様:

- `backend/specs/agent-question-context-preparation-slice.md`
- `backend/specs/question-answering-flow-boundary-refactor-slice.md`
- `backend/specs/agent-history-run-execution-slice.md`
- `backend/specs/agent-threads-runs-boundary-slice.md`
- `backend/specs/agent-live-event-producer-wiring-slice.md`
- `backend/specs/agent-input-safety-gate-slice.md`

本 slice は Runner の境界抽出までを扱う。各 agent の name、instructions、model、tools、
output type を宣言的な `Agent` 定義へ集約する変更は後続 slice とする。

## Work Definition

### Problem

1. `run_agent_answer()` が、永続 run の状態遷移と、question context preparation から
   agent 呼び出しまでの意味的な回答処理の両方を所有している。
2. ユーザーの元質問、履歴、整理済み `QuestionContext`、前回回答、基準時刻の
   関係が局所変数と `AnswerQuestionInput` の組み立てに分散している。
3. context preparation 完了前と完了後が型で区別されておらず、「回答を開始できる
   context に何がそろっているか」を呼び出し側が知る必要がある。
4. worker task を通さないと、「質問文脈を1回準備し、同じ文脈で回答し、
   final output を返す」というユースケース単体をテストしにくい。
5. 将来 agent 宣言を集約しても、それをユーザー入力から実行する一貫した
   呼び出し口がなければ、全体フローの見通しは改善しない。

### Evidence

- `backend/app/queue/tasks/agent_run.py::run_agent_answer()` は、run attempt 取得、
  Redis/SSE publisher 初期化、履歴読み込み、`QuestionContextService.prepare()`、
  agent graph 構築、`agent.answer()`、例外分類、DB 完了処理、terminal 通知を
  同じ task 内で行う。
- 同 task 内で `as_of` を1回確定し、question context generator と
  `AnswerQuestionInput` の両方へ同じ値を渡している。
- `_latest_assistant_answer()` は bounded history の最新 assistant 本文を無加工で
  `previous_answer` へ渡す。
- `QuestionContextService.prepare()` は `QuestionContextPreparationResult` を返し、
  完成した `QuestionContext` と観測用 `QuestionContextTelemetry` を分けている。
- `QuestionAnsweringOrchestrator.answer()` は同じ `QuestionContext` から
  `PlanningRequest` と `AnsweringRequest` を作り、planner と direct/evidence answerer へ渡す。
- `QuestionAnsweringAgent` は `answer(AnswerQuestionInput) -> AnswerQuestionResult` だけを
  公開する既存 Protocol である。
- `backend/app/agent/composition.py` は provider adapter、search capability、flow、
  orchestrator を worker 実行時に組み立て、AI SDK の lazy import を維持する。
- `backend/app/agent/runs/` は Postgres の run lifecycle、repository、projection を所有する
  既存 package であり、in-process の agent execution 境界ではない。
- OpenAI Agents SDK の Runner は starting agent と input を受け、1回の呼び出しを
  chat conversation 上の1つの logical turn として扱う。
- OpenAI Agents SDK は local context と LLM-visible context を別概念とし、local context は
  そのまま LLM へ送られないと明記している。

### Invariants

- Postgres の `AgentRun` と関連 message/source row を、run 状態と最終回答の SSoT とする。
- `Runner` は DB、SQLAlchemy、Taskiq、Redis、HTTP client、transaction、terminal 通知を
  知らない。
- worker は attempt 取得、冪等ガード、履歴 DB query、run 完了/失敗永続化、
  terminal 通知とその順序を引き続き所有する。
- `QuestionContextService.prepare()` は1つの `Runner.run()` invocation で最大1回だけ
  呼ぶ。同じ persistent `AgentRun` の再 attempt は別 invocation として扱う。
- Runner は invocation 間で `QuestionContext`、history、previous answer、`as_of` を cache/再利用しない。
- context preparation 完了後にだけ `AnsweringRunContext` を構築する。
- `QuestionContext | None` のような「未準備かもしれない」field を run context に
  持たせない。
- context preparation が返した同じ `QuestionContext` instance を
  `AnswerQuestionInput` へ投影し、planner と answerer へ引き継ぐ。
- `QuestionContextTelemetry` は観測専用とし、`AnsweringRunContext`、`RunHooks`、
  `RunResult`、`AnswerQuestionInput` に混ぜず、planner/answerer の判断材料にしない。
- `RunContext`、`AnsweringRunContext` 全体を LLM へ送らない。LLM に必要な
  `QuestionContext` は既存 request/prompt 経路で明示的に渡す。
- bounded history の順序、件数、本文、`missing_aspects` を Runner が書き換えない。
- `RunInput`、`AnsweringRunContext`、`previous_answer` を log、metric attribute、
  `QuestionResolvedEvent` に一括 serialize しない。
- 1つの `Runner.run()` invocation を固定名 `agent_answering_run` の span 1件で囲み、
  context preparation、hook、starting agent、`RunResult` 構築を同じ span 内で実行する。
- `agent_answering_run` の明示 attribute は文字列化した内部 `run_id` だけとし、
  質問本文、履歴、`QuestionContext`、`previous_answer`、prompt、final output、`as_of` を載せない。
- `AnswerGenerationStopped` は正常な停止制御として span を error にせず閉じ、その後で
  同じ例外 instance を worker へ再送出する。それ以外の貫通例外は span の error 記録を維持する。
- `previous_answer` は bounded history 内の最新 assistant 本文とし、該当がなければ
  空文字列とする。
- `as_of` は execution attempt ごとに1回確定し、context preparation と回答処理で
  同じ値を使う。
- `run_id` / `as_of` を model-visible `QuestionContext` に混ぜない。
- context preparation と `on_answering_context_prepared` を、回答用 starting agent の
  具象 graph、safe HTTP client、provider/search runtime の構築より前に実行する。
- `RunHooks` に `RunInput` や raw history snapshot を渡さず、callback が必要とする
  元質問、履歴の有無、準備済み `QuestionContext` だけを渡す。
- hook が例外を送出した場合は同じ例外を伝播し、starting agent を呼ばない。
- deferred starting agent が開いた safe HTTP client は、graph 構築または回答処理の
  成否にかかわらず、`answer()` invocation ごとに解放する。
- direct / internal / external / mixed の分岐は既存どおり
  `QuestionAnsweringOrchestrator` の Python `match` が所有する。
- provider、retry、fallback、validation、audit、metrics、progress/delta/continuation の振る舞いと
  実行順を変更しない。
- typed error を Runner 独自の error code へ変換せず、現在の worker の分類境界まで
  そのまま伝播する。
- API response、DB schema、Redis event schema、認証・認可、prompt、model 設定、
  external provider 呼び出しを変更しない。

### Non-goals

- `openai-agents` dependency を追加すること。
- OpenAI Agents SDK の class/signature を互換 API として再実装すること。
- model が tool、handoff、次の agent を自由に選ぶ agent loop を導入すること。
- `max_turns`、`RunState`、`previous_response_id`、`conversation_id`、`session`、
  `auto_previous_response_id`、generic `RunConfig` / `error_handlers` を追加すること。
- Postgres にある thread history と別の conversation persistence を追加すること。
- `QuestionContext`、`AnswerQuestionInput`、`AnswerQuestionResult` の意味や field を変更すること。
- `QuestionAnsweringOrchestrator` の分岐と回答組み立てを Runner へ移すこと。
- input safety policy や block persistence を実装すること。
- agent の name、instructions、model、tools、output type を宣言クラスへ集約すること。
- sync runner、streaming runner、resume API を追加すること。
- worker の progress、activity、delta、continuation、terminal を1つの generic hook に統合すること。
- planning、retrieval、synthesis ごとの明示的な child span を追加すること。
- DB 完了/失敗永続化や terminal 通知を `agent_answering_run` span 内へ移すこと。

### Done

- `Runner.run()` が、raw question + bounded history + run-local context から
  `QuestionContext` を準備し、starting agent を1回実行し、`RunResult` を返す。
- 準備完了後の状態が `AnsweringRunContext` として型化され、未準備状態を
  optional field で表さない。
- worker task から question context preparation、previous answer 決定、
  `AnswerQuestionInput` 組み立てが除かれ、Runner 呼び出しに置き換わる。
- Runner に渡す starting agent は軽量な遅延実行 adapter とし、context preparation と
  hook 完了前に回答用 starting agent の具象 graph、HTTP client、provider/search runtime を
  構築しない。
- worker の DB/Redis/Taskiq/error mapping/transaction/terminal 責務と既存振る舞いが維持される。
- `question.resolved` の発火条件と通知経路が run hook の所有テストで維持される。
- run hook が raw history を受け取らず、hook 例外時に starting agent を起動しない。
- deferred starting agent が正常終了、graph 構築失敗、回答失敗のすべてで safe HTTP client を
  解放する。
- Runner 単体テストが DB、Redis、HTTP、実 provider なしで通る。
- `Runner.run()` 全体が `agent_answering_run` span で囲まれ、既存の外側 span を親として
  context preparation、hook、starting agent の処理が同じ trace 配下に入る。
- `agent_answering_run` span attribute に質問本文、履歴、prompt 相当のモデル可視テキストが
  含まれないことを実 exporter で検証する。
- API schema、DB migration、dependency lockfile、generated frontend type に差分がない。
- 実装後に `/check` が完走する。

## 用語と責務境界

### `AgentRun`

Postgres に永続化される run 状態機械。`queued | running | completed | failed` と、
assistant message/source への関連を持つ。`backend/app/agent/runs/` が所有する。
原則として、1つの `AgentRun` は1つの user turn を表す。

### `PreparedAgentRun`

worker が `AgentRun` の attempt を取得した直後の DB snapshot。`run_id`、`thread_id`、
`question`、`user_message_seq`、`attempt_epoch` を持つ。履歴取得と永続 run lifecycle の
ための型であり、`AnsweringRunContext` と統合・rename しない。
同じ `AgentRun` を再取得した場合は `attempt_epoch` が増え、Runner も別 invocation として
再実行され得る。

### `Runner`

1つの persistent `AgentRun` の1 execution attempt を進行する in-process の
application execution boundary。question context preparation と starting agent 呼び出しの
順序を所有するが、run の永続状態機械は所有しない。

1つの invocation が成功すれば1つの logical user turn の回答を生成する。ただし、
worker crash 等で同じ `AgentRun` が再 attempt された場合、1つの logical turn に対して
`Runner.run()` が複数回呼ばれ得る。

### `RunInput`

Runner に渡す Vector 固有の入力。現在の質問と、worker/repository が読み込んだ
bounded・oldest-first の thread history snapshot を持つ。conversation の永続手段や
Responses API input item ではない。

### `RunContext`

context preparation より前から存在する invocation-local metadata。`run_id` と
`as_of` を持つ。これらは code/hook の実行文脈であり、LLM に見せる
質問文脈ではない。

`attempt_epoch` は worker が live publisher / continuation probe に直接 bind する既存値であり、
Runner が消費しないため `RunContext` に重複保持しない。

### `AnsweringRunContext`

context preparation 完了後にだけ作られる「回答処理の準備が整った run context」。

`Answering` は使用目的、`Run` は現在の Runner invocation、`Context` はその
execution attempt で共有する情報を表す。`Prepared` のような処理状態だけではなく、
「回答に使う」というドメイン上の目的が名前から読めることを優先する。

### `QuestionContext`

thread history と現在の質問から準備された、planner/answerer 用の意味的な質問文脈。
standalone question、content/response requirements、prior coverage、active goal を持つ。
`AnsweringRunContext.question_context` の一部だけが、既存 request/prompt 経路で
LLM-visible になる。

### `RunResult`

Runner が成功したときの結果。starting agent の `final_output` と、それを作った
`AnsweringRunContext` を持つ。呼び出し元は final output だけでなく、どの
prepared context から生成したかを同じ結果境界で確認できる。

### `RunHooks`

Runner 内の lifecycle を観測する callback 境界。最初の slice では
`on_answering_context_prepared()` だけを持ち、処理分岐、結果変更、永続化、
continuation 制御を行わない。

## Context visibility

| 型 / 値 | 主な consumer | LLM へ自動送信 | 責務 |
|---|---|---:|---|
| `RunInput.question` | context preparer / Runner / hook | しない | 現在の生のユーザー質問 |
| `RunInput.history` | context preparer / Runner | しない | bounded thread snapshot |
| `RunContext` | Runner | しない | persistent run identity、attemptの基準時刻 |
| `AnsweringRunContext` | Runner / `RunResult` | しない | 回答開始に必要な準備済み情報 |
| `QuestionContext` | hook / planner / answerer | 明示的に prompt へ投影 | 今回何をどう答えるか |
| `QuestionContextTelemetry` | `QuestionContextService` | しない | 既存 context preparation の観測 |
| `AnswerQuestionResult` | worker / persistence mapper | 対象外 | 最終回答の domain result |

`context` という名前の object を Runner へ渡しただけでは、その内容は LLM に見えない。
LLM に使わせる情報は、引き続き `PlanningRequest` / `AnsweringRequest` から prompt へ
明示的に含める。

`RunHooks` には `RunInput` と `AnsweringRunContext` を渡さない。Runner が元質問、履歴の有無、
準備済み `QuestionContext` だけを投影し、raw history の本文、role、`missing_aspects`、
`previous_answer` を callback 境界へ公開しない。

## 現在の実行フロー

```text
run_agent_answer()
  ├─ acquire AgentRun attempt
  ├─ initialize live publishers / reporters / continuation
  ├─ read bounded thread history
  ├─ build QuestionContextGenerator
  ├─ QuestionContextService.prepare
  ├─ maybe publish question.resolved
  ├─ build QuestionAnsweringAgent
  ├─ construct AnswerQuestionInput
  ├─ agent.answer
  ├─ map exception to persistent error code
  ├─ persist final result
  └─ publish terminal event
```

## 目標フロー

```text
run_agent_answer()                         # persistent run coordinator
  ├─ acquire AgentRun attempt
  ├─ initialize live publishers / reporters / continuation
  ├─ read bounded thread history
  ├─ [future] InputSafetyService -> allow
  ├─ build Runner / deferred starting_agent / RunHooks
  ├─ Runner.run(...)
  │    ├─ QuestionContextService.prepare
  │    ├─ construct AnsweringRunContext
  │    ├─ RunHooks.on_answering_context_prepared
  │    ├─ project AnswerQuestionInput
  │    ├─ deferred starting_agent.answer
  │    │    ├─ open safe HTTP client
  │    │    ├─ build concrete QuestionAnsweringAgent graph
  │    │    ├─ concrete agent.answer
  │    │    └─ close safe HTTP client
  │    └─ return RunResult
  ├─ map propagated exception to persistent error code
  ├─ persist RunResult.final_output
  └─ publish terminal event after commit
```

## Trace hierarchy

Taskiq の OpenTelemetry middleware が作る task span を親とし、Runner は意味的な回答処理だけを
固定名の child span で囲む。明示的に追加する trace 階層は次のとおりである。

```text
execute/run_agent_answer                         # Taskiq middleware
├─ agent_answering_run {run_id}                  # Runner.run() 全体
│  ├─ [code region / spanなし]
│  │  └─ prepare -> hook -> deferred starting_agent.answer
│  └─ [descendant spans]
│     └─ existing provider / HTTP / DB spans     # instrumentation が生成する場合
├─ persist RunResult.final_output                # Runner 成功後、worker が所有
└─ publish terminal event                        # commit 後、worker が所有
```

`agent_answering_run` の span 名へ `run_id` や質問を埋め込まず、低 cardinality の固定名を使う。
明示 attribute は persistent run と関連づける `run_id: str` のみとする。次の値は attribute に
載せない。

- `RunInput.question` と `RunInput.history`。
- `AnsweringRunContext.question_context` と `previous_answer`。
- planner / answerer の prompt、provider response、`RunResult.final_output`。
- `as_of` や provider/search の動的な入力値。

Python の active span context は `await` をまたいで伝播するため、具象 graph の構築や
`QuestionAnsweringOrchestrator` の処理を Runner file へ物理的に移さなくても、
`starting_agent.answer()` の内側で生成される既存 instrumentation span は
`agent_answering_run` の子になる。planning / retrieval / synthesis を個別に可視化する
child span は、各 phase の所有境界を再設計する後続 slice で扱う。
テストで使う prepare / hook / starting agent の probe span は包含関係を検証するためだけの
test double であり、production に同名の span を追加しない。

## Responsibility model

| 責務 | worker | Runner | QuestionAnsweringOrchestrator | composition |
|---|:---:|:---:|:---:|:---:|
| persistent attempt 取得 / 冪等ガード | ○ | - | - | - |
| bounded history DB query | ○ | - | - | - |
| run-local metadata の作成 | ○ | 受け取る | - | - |
| question context preparation | - | ○ | - | 依存構築 |
| latest previous answer の決定 | - | ○ | - | - |
| `AnsweringRunContext` の構築 | - | ○ | - | - |
| answering context prepared hook | hook を構築・注入 | 呼び出す | - | - |
| planning / route dispatch | - | - | ○ | 依存構築 |
| evidence collection / answer assembly | - | - | ○ | 依存構築 |
| deferred starting agent / HTTP lifecycle | - | 呼び出すだけ | - | ○ |
| provider/search client の具象配線 | - | - | - | ○ |
| progress / activity / delta / continuation | sink/control を注入 | 統合しない | 既存経路 | 配線 |
| exception -> persistent error code | ○ | 伝播 | 伝播 | - |
| DB complete/fail transaction | ○ | - | - | - |
| terminal publish | ○ | - | - | - |

## 設計判断

### 1. `running` package を in-process execution 境界とする

```text
backend/app/agent/running/
├── __init__.py
├── contract.py
├── hooks.py
└── runner.py
```

- `runs/` は Postgres の persistent run lifecycle。
- `running/` は実行中の1回の回答ユースケース。
- `evidence_collection/external_search/runner.py` の `ExternalSearchResearchRunner` は外部調査内の
  nested runner として維持し、本 slice で rename しない。
- import 時に provider SDK、SQLAlchemy、Taskiq、Redis を読み込まない。

`running` は、現在実行する動作を表し、DB entity 集合である `runs` と区別する。

### 2. `RunInput` は Vector 固有の immutable input とする

```python
@dataclass(frozen=True, slots=True)
class RunInput:
    question: str
    history: tuple[ThreadMessageSnapshot, ...]
```

- `question` は API schema で検証・strip 済みの現在の質問。
- `history` は worker が current user message より前から読んだ最大6件の
  oldest-first snapshot。
- Runner は history の DB query、追加読み込み、message 切り詰めを行わない。
- `QuestionContextService` 内の prompt 用 char cap / missing-aspect normalization は維持する。
- list ではなく tuple で受け、Runner の lifecycle 中に呼び出し側から
  history 要素を差し替えられないようにする。

`RunInput` は API trust boundary の runtime validator ではない。次は呼び出し元の precondition とする。

- `question` は非空・strip 済み・最大1,000文字である。
- `history` は最大6件・oldest-firstである。
- 各 snapshot は repository が構築した `ThreadMessageSnapshot` である。

これらを `RunInput` で再検証すると API schema / repository query と同じ制約の
SSoT が増えるため、初期 slice で `__post_init__` や Pydantic validator を追加しない。

### 3. context preparation 前後を別の型で表す

```python
@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class AnsweringRunContext:
    run_context: RunContext
    question_context: QuestionContext
    previous_answer: str
```

`RunContext` は context preparation 前から有効である。`AnsweringRunContext` は
`QuestionContextService.prepare()` が正常結果または既存の safe fallback を返した後に
だけ構築する。

次のような単一 mutable context は採用しない。

```python
@dataclass
class RunContext:
    question_context: QuestionContext | None = None
```

この形は、後続処理のすべてに「本当に準備済みか」の確認を強い、
未準備状態を型が許容するためである。

`run_id` は run 相関と context preparation 失敗 log 用、`as_of` は現在の
execution attempt の共通基準時刻である。これらは `AnswerQuestionInput` や
prompt へ一括投入しない。

### 4. `RunResult` は final output と answering context だけを持つ

`QuestionContextTelemetry` は、explicit feedback や前回不足の有無を観測するための値であり、
planner/answerer の入力ではない。そのため、下流の回答処理用 context に混ぜない。

```python
@dataclass(frozen=True, slots=True)
class RunResult:
    final_output: AnswerQuestionResult
    context: AnsweringRunContext
```

`QuestionContextService` は既存どおり context outcome metric を自身で記録する。
初期 slice に telemetry の新しい consumer がないため、Runner は preparation result の
telemetry を hook、`AnsweringRunContext`、`RunResult`、`AnswerQuestionInput` へ公開しない。

`RunResult.context` は、「final output がどの準備済み context から作られたか」を
Runner の戻り値として保証するための contract であり、telemetry の将来拡張用ではない。

### 5. `Runner` は DI 可能な instance とする

OpenAI Agents SDK の呼び出し形は参考にするが、Vector で class-level default runner や
global dependency を追加しない。Runner が必要とする context preparation capability を
`QuestionContextPreparer` として constructor injection した instance method とする。

```python
class QuestionContextPreparer(Protocol):
    async def prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> QuestionContextPreparationResult: ...


class Runner:
    def __init__(
        self,
        *,
        context_preparer: QuestionContextPreparer,
    ) -> None: ...

    async def run(
        self,
        starting_agent: QuestionAnsweringAgent,
        input: RunInput,
        *,
        run_context: RunContext,
        hooks: RunHooks | None = None,
    ) -> RunResult: ...
```

`run_context` は optional にしない。`run_id` と `as_of` は Vector の persistent run attempt と
回答処理を関連づける必須値である。`as_of` は worker が UTC-aware `datetime` として
構築する precondition とし、Runner で再検証しない。

Runner の実行順序は次に固定する。

```python
async def run(...):
    preparation = await self._context_preparer.prepare(
        question=input.question,
        history=list(input.history),
        as_of=run_context.as_of,
        run_id=run_context.run_id,
    )
    answering_context = AnsweringRunContext(
        run_context=run_context,
        question_context=preparation.context,
        previous_answer=_latest_assistant_answer(input.history),
    )
    if hooks is not None:
        await hooks.on_answering_context_prepared(
            original_question=input.question,
            has_history=bool(input.history),
            question_context=answering_context.question_context,
        )
    final_output = await starting_agent.answer(
        AnswerQuestionInput(
            context=answering_context.question_context,
            as_of=answering_context.run_context.as_of,
            previous_answer=answering_context.previous_answer,
        )
    )
    return RunResult(
        final_output=final_output,
        context=answering_context,
    )
```

Runner は starting agent を1回呼び出す。現行の `QuestionAnsweringOrchestrator` 内で
何回 LLM/provider を呼ぶかは、この「starting agent 呼び出し1回」とは別概念である。

### 6. `AnswerQuestionInput` は既存 agent port への投影として維持する

`AnsweringRunContext` 全体を `QuestionAnsweringAgent.answer()` へ渡さず、現行の
`AnswerQuestionInput` に必要な値だけを投影する。

```text
AnsweringRunContext
  ├─ run_context.run_id             # local only
  ├─ run_context.as_of              ─┐
  ├─ question_context       ├─ AnswerQuestionInput
  └─ previous_answer        ─┘
```

- `run_id` は agent core に渡さない。
- `as_of`、`question_context`、`previous_answer` だけを現行 port へ渡す。
- `AnswerQuestionInput.context is AnsweringRunContext.question_context` を保ち、再構築や
  serialize/deserialize を挟まない。
- `AnswerQuestionInput` 自体の rename と field 変更は後続の agent declaration 設計まで行わない。

### 7. `RunHooks` は answering context preparation の観測だけから始める

```python
class RunHooks(Protocol):
    async def on_answering_context_prepared(
        self,
        *,
        original_question: str,
        has_history: bool,
        question_context: QuestionContext,
    ) -> None: ...
```

この method は `AnsweringRunContext` が完成し、starting agent を呼べる状態になった後で
呼ばれる。ただし、hook にはその object 全体を渡さず、`QuestionContext` だけを投影する。
`original_question` と `has_history` は `question.resolved` の発火条件だけに使い、raw
`RunInput.history` と `previous_answer` を hook 引数として追加しない。

hook は次を行わない。

- `QuestionContext` と `final_output` の置換。
- starting agent を呼ぶかどうかの分岐。
- run の DB 完了/失敗遷移。
- terminal event の発行。
- answer generation の continuation 判定。

初期の具象 hook は既存 `question.resolved` 通知だけを所有する。

```python
class QuestionResolvedRunHooks:
    def __init__(self, *, events: AnswerEventReporter) -> None: ...

    async def on_answering_context_prepared(
        self,
        *,
        original_question: str,
        has_history: bool,
        question_context: QuestionContext,
    ) -> None:
        if not has_history:
            return
        if (
            question_context.standalone_question.strip()
            == original_question.strip()
        ):
            return
        await self._events.event_occurred(
            QuestionResolvedEvent(
                standalone_question=(
                    question_context.standalone_question
                )
            )
        )
```

これにより、次の既存条件を維持する。

- history あり + standalone question が変化: 1回発行。
- history なし: generator が rewrite を返しても0回。
- history あり + echo/safe fallback: 0回。

`AnswerEventReporter` の既存契約どおり、reporting sink の運用失敗は呼び出し元へ
伝播させない。Runner はあらゆる hook 例外を blanket catch して programming defect を
隠すことはしない。hook 自体が例外を送出した場合は、その例外を変換せず伝播し、
starting agent を呼ばずに現在の Runner invocation を短絡する。

progress、search activity、answer delta、continuation、terminal は失敗契約と control 責務が
異なるため、初期 `RunHooks` へ取り込まない。

### 8. `previous_answer` の決定を Runner が所有する

Runner は `RunInput.history` を後ろから走査し、最初の assistant message 本文を
無加工で `AnsweringRunContext.previous_answer` へ入れる。assistant message がなければ
`""` とする。

- `QuestionContextService` が prompt 用に cap した history から取得しない。
- `relevant_prior_coverage` で代替しない。
- summary、trim、citation 除去、source 復元を行わない。
- 引き続き direct answer 経路だけがこの値を使い、evidence answer 経路へは
  `QuestionAnsweringOrchestrator` が渡さない。

### 9. Runner は error handler にならない

| 条件 | Runner | worker |
|---|---|---|
| context generator の既知失敗 | `QuestionContextService` の safe fallback で継続 | final result を通常保存 |
| `AnswerGenerationStopped` | そのまま伝播 | 現行どおり return |
| `AIProviderConfigurationError` | そのまま伝播 | `generation_unavailable` |
| `AIProviderError` | そのまま伝播 | `generation_unavailable` |
| `DirectAnswerInvalidError` | そのまま伝播 | `generation_unavailable` |
| 想定外例外 | そのまま伝播 | `internal_error` |

Runner は retry、fallback、exception wrapping、persistent error code への変換を追加しない。
provider 内部と `QuestionContextService` / answer flow が現在所有する retry/fallback は
そのまま維持する。

### 10. worker は persistent run coordinator として残す

`run_agent_answer()` は Runner 抽出後も次を所有する。

1. `PreparedAgentRun` 取得と idempotent skip。
2. Redis live attempt、activity/stage/delta reporter、continuation probe の構築。
3. current user message より前の bounded history DB query。
4. execution attempt ごとの `RunContext(run_id, as_of)` の構築。
5. Runner、遅延 starting agent、`QuestionResolvedRunHooks` の構築と注入。
6. Runner から伝播した例外の分類。
7. `RunResult.final_output` の完了 transaction。
8. DB commit 後の terminal publish。

worker は `RunResult.context` を再解釈して回答を変更しない。
Runner の戻り値から `final_output` だけを既存 persistence mapper へ渡す。

### 11. composition は Runner と遅延 starting agent を別々に構築する

```python
def build_runner() -> Runner: ...


def build_question_answering_starting_agent(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    progress: AnswerProgressReporter | None = None,
    events: AnswerEventReporter | None = None,
    delta_reporter: AnswerDeltaReporter | None = None,
    continuation: AnswerGenerationContinuation | None = None,
) -> QuestionAnsweringAgent: ...


def build_question_answering_agent(
    *,
    tavily_client: TavilyHttpClient,
    ...,
) -> QuestionAnsweringAgent: ...
```

`build_runner()` は `QuestionContextService` と generator を構築する。現在 worker にある
次の safe fallback 配線を保つ。

```python
try:
    generator = build_question_context_generator()
except (AIProviderConfigurationError, AIProviderError):
    generator = None
return Runner(
    context_preparer=QuestionContextService(generator=generator),
)
```

`build_question_answering_starting_agent()` が返す object は
`QuestionAnsweringAgent` Protocol を満たすが、
構築時に safe HTTP client や具象 answer graph を作らない。`answer()` が呼ばれた時点で
だけ、現行の resource lifecycle と graph builder を開く。factory は worker から渡された
`session_factory`、reporter、continuation だけを保持する。`tavily_client` は引数に取らず、
各 `answer()` invocation の中で生成する。

```python
class _DeferredQuestionAnsweringAgent:
    async def answer(
        self,
        input: AnswerQuestionInput,
    ) -> AnswerQuestionResult:
        async with make_safe_async_client() as tavily_client:
            agent = build_question_answering_agent(
                session_factory=self._session_factory,
                tavily_client=tavily_client,
                progress=self._progress,
                events=self._events,
                delta_reporter=self._delta_reporter,
                continuation=self._continuation,
            )
            return await agent.answer(input)
```

`_DeferredQuestionAnsweringAgent` は composition 内の private・Vector固有 adapter とし、
generic lazy agent abstraction や public factory Protocol へ広げない。この adapter の目的は、
Runner API を `starting_agent` のまま保ちながら、次の現行順序を守ることに限定する。

```text
QuestionContextService.prepare
  -> question.resolved hook
  -> safe HTTP client open
  -> concrete QuestionAnsweringAgent graph build
  -> concrete agent.answer
  -> safe HTTP client close
```

safe HTTP client は deferred agent の field として invocation 間で保持しない。正常終了、
concrete graph builder の例外、concrete agent の `answer()` 例外のいずれでも、例外または
結果を呼び出し元へ返す前に `async with` を抜けて解放する。

- context generator の provider/configuration 構築失敗だけを `None` に縮退する。
- concrete starting agent 構築時の configuration error は縮退せず、worker の既存
  `generation_unavailable` 分類へ伝播する。
- context preparation が想定外例外で失敗した場合、safe HTTP client と concrete agent graph を
  構築しない。
- deferred agent の構築だけでは `make_safe_async_client()` と
  `build_question_answering_agent()` を呼ばない。
- provider adapter の import は builder 関数内の lazy import のままとする。
- API process は Runner、deferred agent、AI graph を import 時に構築しない。

`QuestionResolvedRunHooks` は worker が既存 `activity_reporter` を渡して構築する。
Redis publisher の具象型を `running` package へ渡さない。

### 12. input safety の allow 後に起動できる境界を維持する

`agent-input-safety-gate-slice.md` は、`allow` 確定前にQuestion Context以降のprovider runtime、
Tavily client、Planner、retrieval、answer Agentを起動しないことを要求する。

2026-07-20のAgent / Runner責務移行により、将来配置は
`agent-input-safety-gate-slice.md`の更新仕様が本節を上書きする。Input Safety Agentは
`AnsweringRunner.run()`の明示的な先頭phaseとし、workerはsemantic phaseの順序を所有しない。
input safety実装後の順序は次である。

```text
acquire -> history -> build AnsweringRunner -> Runner.run
  ├─ input safety block  -> raise InputSafetyBlocked
  │                         -> worker marks run policy_blocked -> terminal
  ├─ input safety failed -> failed(generation_unavailable)
  └─ input safety allow  -> question context -> planning -> retrieval -> answer
```

input safetyをmiddlewareへ隠さず、Runnerの主制御から最初の工程として読める形にする。
RunnerとimmutableなAgent宣言はsafety前に構築できるが、allow確定前にQuestion Context以降の
provider runtime、Tool client、answering phasesをactivateしない。

### 13. OpenAI Agents SDK の全引数を複製しない

| SDK の概念 | 初期 Vector Runner | 判断 |
|---|---|---|
| `starting_agent` | 採用 | context-first を保つ軽量な deferred agent を渡す |
| `input` | `RunInput` として採用 | Vector の question + bounded history に限定 |
| `context` | `run_context: RunContext` として採用 | run-local identity/time を共有 |
| `hooks` | `on_answering_context_prepared` だけ採用 | 現在存在する観測境界に限定 |
| `RunResult` | 採用 | final output + prepared answering context |
| `max_turns` | 採用しない | model-driven agent loop がない |
| `run_config` | 採用しない | 変更可能な run 共通設定の consumer がない |
| `error_handlers` | 採用しない | worker の typed error mapping を維持 |
| `RunState` | 採用しない | approval/resume 契約がない |
| `session` | 採用しない | Postgres thread history が SSoT |
| `conversation_id` | 採用しない | OpenAI server-managed state を使わない |
| `previous_response_id` | 採用しない | provider 中立の DB history 経路を維持 |

conversation state は一つの方式に限定する。本 slice では既存の Postgres thread/message を
継続し、client-managed history と provider-managed continuation の重複投入を作らない。

## 依存方向

```text
queue/tasks/agent_run.py
  ├─ agent/runs                 # persistent lifecycle
  ├─ agent/threads              # bounded history projection
  ├─ agent/live_updates         # Redis/SSE/reporters
  ├─ agent/composition          # concrete builders
  └─ agent/running              # Runner + contracts + hooks
          ├─ agent/question_context
          └─ agent/contract     # QuestionAnsweringAgent / answer I/O

agent/composition
  ├─ agent/running.Runner
  ├─ agent/question_context.QuestionContextService
  └─ provider/search/flow concrete implementations

agent/running does not depend on
  - agent/runs
  - agent/threads.repository
  - agent/live_updates concrete publishers
  - app.models
  - SQLAlchemy / Taskiq / Redis / HTTP client
```

`QuestionAnsweringOrchestrator` は `running` package を import しない。Runner が既存 agent port を
呼び、orchestrator は回答ユースケース内の planning/dispatch に専念する。

## API / DB / dependency

本 slice は公開 contract と persistent schema を変更しない。

- FastAPI request/response schema: 変更なし。
- OpenAPI / generated frontend types: 変更なし。
- DB model / Alembic migration: 変更なし。
- Redis stream/list event schema: 変更なし。
- Taskiq message schema: 変更なし。
- authentication / authorization: 変更なし。
- Python dependency / lockfile: 変更なし。

`RunInput`、`RunContext`、`AnsweringRunContext`、`RunResult`、`RunHooks` は backend 内部の
application contract であり、OpenAPI に露出しない。

## Expected file changes

```text
backend/app/agent/running/__init__.py                    # 新規: public internal exports
backend/app/agent/running/contract.py                    # 新規: run input/context/result/hooks/preparer port
backend/app/agent/running/hooks.py                       # 新規: question.resolved hook adapter
backend/app/agent/running/runner.py                      # 新規: Runner + answering run span
backend/app/agent/composition.py                         # build_runner() + deferred starting agent + lazy DI
backend/app/queue/tasks/agent_run.py                     # semantic block を Runner 呼び出しへ置換
backend/tests/agent/running/__init__.py                  # 新規
backend/tests/agent/running/test_contract.py             # 新規: phase contracts
backend/tests/agent/running/test_runner.py               # 新規: pure Runner tests
backend/tests/agent/running/test_hooks.py                # 新規: question.resolved ownership
backend/tests/agent/test_composition.py                  # 新規: deferred agent resource lifecycle
backend/tests/agent/test_agent_run_task.py               # worker integration regression を更新
```

`backend/app/agent/contract.py` の `AnswerQuestionInput` / `QuestionAnsweringAgent` は変更しない。
`backend/app/agent/question_context/` の出力 contract/prompt/provider 設定も変更しない。

## Tests

### Runner contract / unit tests

1. `RunInput` が question と tuple history を保持する。
2. `RunContext` が `run_id` / `as_of` を保持する。
3. `AnsweringRunContext` が `QuestionContext | None` を持たず、構築時に完成済み
   `QuestionContext` を必須とする。
4. Runner が context preparer を1回だけ呼び、question、history、`run_id`、`as_of` を
   欠落・書き換えなく渡す。
5. history の最新 assistant 本文が `previous_answer` に無加工で入る。
6. assistant history がない場合、`previous_answer == ""` となる。
7. starting agent を1回だけ呼び、`AnswerQuestionInput.context` が準備された
   `QuestionContext` と同一 instance である。
8. `AnswerQuestionInput.as_of` / `previous_answer` が `AnsweringRunContext` と同値である。
9. `on_answering_context_prepared` を starting agent より前に1回呼び、元質問、
   `has_history`、同じ `QuestionContext` だけを渡し、`RunInput`、raw history、
   `previous_answer` を渡さない。
10. hook が例外を送出した場合、その同じ例外を wrapping/conversion せず伝播し、
    starting agent を呼ばない。
11. `RunResult.final_output` が agent result そのもの、`RunResult.context` が agent input の
    投影元と同じ object であり、その `question_context` が hook に渡した object と同一である。
12. context preparer の想定外例外、starting agent 例外、`AnswerGenerationStopped` を
    wrapping/conversion せず伝播する。
13. Runner 単体テストが DB、Redis、HTTP、実 provider なしで実行できる。
14. 同じ Runner instance の2回目 invocation で context preparation を再実行し、
    1回目の `AnsweringRunContext` を再利用しない。
15. 外側に active span がある場合、`agent_answering_run` がその child となり、context
    preparation、hook、starting agent 内で生成した probe span がすべてその child になる。
16. `agent_answering_run` の明示 attribute が `run_id` だけであり、質問本文、user/assistant
    履歴、`QuestionContext` の各モデル可視 field、previous answer、final output の識別可能な
    文字列を含まない。
17. `AnswerGenerationStopped` が同じ例外 instance のまま伝播する一方、
    `agent_answering_run` に exception event と error level を記録しない。

### Hook tests

1. history があり standalone question が変化したときだけ
   `QuestionResolvedEvent` を1回通知する。
2. initial question、echo、safe fallback で通知しない。
3. 通知する standalone question は完成済み `QuestionContext` の値である。
4. raw history 本文を event payload へ追加しない。

### Composition / deferred starting agent tests

1. `build_question_answering_starting_agent()` の呼び出しだけでは safe HTTP client と
   concrete agent graph を構築しない。
2. 正常終了時は `client enter -> graph build -> agent.answer -> client exit` の順となり、
   final result を変更せず返す。
3. concrete graph builder が例外を送出しても client を解放し、同じ例外を伝播する。
4. concrete agent の `answer()` が例外を送出しても client を解放し、同じ例外を伝播する。
5. 同じ deferred agent の2回目の `answer()` では新しい client を開き、invocation ごとに
   1回ずつ解放する。

### Worker integration regressions

1. acquire skip 後は Runner、starting agent、live dependency を構築しない。
2. worker が `PreparedAgentRun` から `RunContext.run_id` を作り、execution attempt ごとに
   1回確定した `as_of` を Runner へ渡す。`attempt_epoch` は既存どおり
   live publisher / continuation probe に直接 bind する。
3. repository の bounded oldest-first history を tuple 化して `RunInput` へ渡す。
4. `RunResult.final_output` が現行 transaction で assistant message/source へ保存される。
5. completed terminal は DB commit 後にだけ発行される。
6. transition race で complete できない場合、terminal を発行しない。
7. `AIProviderError` / `DirectAnswerInvalidError` は `generation_unavailable`、想定外は
   `internal_error`、`AnswerGenerationStopped` は現行どおり return となる。
8. live attempt begin と activity/stage/delta/continuation の既存タイミングを維持する。
9. context generator 構築の provider/configuration 失敗で safe fallback し、通常回答を
   継続する。
10. context preparation と `on_answering_context_prepared` が完了するまで deferred
    starting agent が safe HTTP client と concrete agent graph を構築しない。
11. context preparation の想定外例外で starting agent の resource factory を呼ばない。
12. API import 経路で AI SDK の lazy import 契約を破らない。

### Existing ownership tests

- `backend/tests/agent/answering/test_orchestration.py` の同一 `QuestionContext` 共有テストを
  維持する。
- `backend/tests/agent/question_context/test_service.py` の fallback / history cap / telemetry テストを
  維持する。
- `backend/tests/agent/test_agent_run_task.py` での DB/terminal/error mapping テストを維持する。

## Implementation order

1. `running/contract.py` と contract test を追加する。実 `QuestionContextService` と fake
   preparer の双方を通常の型注釈で `Runner` に注入し、同じ behavior test を通せることを
   確認する。
2. fake context preparer / fake starting agent / fake hooks を使い、hook 例外時の短絡を含む
   Runner の失敗テストを先に書く。
3. `Runner` を実装し、question context preparation、previous answer 決定、
   `AnswerQuestionInput` 投影を worker と並存させず移す。
4. `QuestionResolvedRunHooks` を追加し、発火条件の所有テストを worker test から移す。
5. `composition.py::build_runner()` と deferred starting agent を追加し、context generator
   構築失敗時の safe fallback 配線、context-first の resource ordering、全終了経路での
   safe HTTP client 解放を固定する。
6. `run_agent_answer()` の semantic block を Runner 呼び出しへ置き換える。
7. worker integration、lazy import、orchestrator identity、question context service の既存テストを
   実行する。
8. `/check` を完走し、API/DB/dependency/generated type に意図しない差分がないことを
   確認する。

各 step で旧実装の forwarding alias や二重呼び出しを残さない。question context preparation と
`question.resolved` 発火条件の SSoT を、worker と Runner/hook の両方に作らない。

## Verification

実装後は `/check` を使い、少なくとも次を確認する。

```text
backend unit tests:
  tests/agent/running/
  tests/agent/test_composition.py
  tests/agent/question_context/
  tests/agent/answering/

backend integration regressions:
  tests/agent/test_agent_run_task.py

static verification:
  ruff lint / format check
  lazy AI SDK import
  git diff --check
```

DB schema、API Pydantic schema、dependency を変更しないため、`/migration`、`/api-contract`、
`/gen-types` は不要である。これらに差分が出た場合は、本 slice の範囲を超えた
変更として一度停止する。

## 参考

- [OpenAI Agents SDK: Running agents](https://openai.github.io/openai-agents-python/running_agents/)
  - Runner に starting agent と input を渡し、1回の run を1つの logical chat turn として
    扱う考え方を参考にする。
- [OpenAI Agents SDK: Context management](https://openai.github.io/openai-agents-python/context/)
  - local context と LLM-visible context の区別を参考にする。
- `backend/specs/agent-question-context-preparation-slice.md`
  - `QuestionContext` と telemetry の意味、planner/answerer 境界の SSoT。
- `backend/specs/agent-history-run-execution-slice.md`
  - persistent `AgentRun`、worker lifecycle、error/transaction 契約の SSoT。
