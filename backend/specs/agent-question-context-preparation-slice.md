# Agent question context preparation slice 仕様

## 位置付け

前提仕様: `backend/specs/agent-conversation-context-slice.md`。

既存の `app/agent/question_resolution/` は、thread の直近履歴から質問と会話文脈を生成し、
planner と回答生成へ渡している。本 slice はこの処理を
「質問の曖昧さ解消」ではなく、**planner より前に今回の質問コンテキストを準備する工程**
として定義し直す。

2026-07-11 のユーザー合意:

- planner の前に、履歴から「何を求めているか」「何を説明済みか」「何が未解決か」を
  整理するサービスを置く。
- context preparation はユーザー要求と thread の状態を記述し、検索方法や実行戦略は
  後続 planner が決める。
- `conversation` は package 境界の名称として再導入せず、今回の質問に必要な情報である
  ことを示す `question_context` を採用する。
- cross-thread のプロフィールや嗜好を扱う `user_context` / personalization には広げない。

## 段階実装

本仕様はquestion context preparationの最終状態を記述する。最初のPRは設計判断1のrenameだけを
先行し、既存挙動を変えない。

- `question_resolution` packageと内部Pythonシンボルを `question_context` へ改名する。
- serviceは `QuestionContextService.prepare()`、LLM portは
  `QuestionContextGenerator.generate()` とする。
- context field、history有無によるskip、fallback、prompt本文、LLM設定は維持する。
- `question.resolved`、`vector.agent.question_resolution.outcome`、
  `question_resolution_failed`、Gemini defect codeは互換性のため維持する。
- 設計判断2以降のcontext拡張と観測語彙変更は後続PRで実装する。

## Problem

現行の `question_resolution` は実際には質問文の書き換えだけでなく、回答形式、既回答、
ユーザーの調査の流れまで構造化している。名称から責務を予測しにくく、後続 planner との
境界も「質問を解決する層」と「質問を計画する層」に見えるため、両者の違いが分かりにくい。

また、過去の回答が `missing_aspects` を保存していても、context preparation が読む
`ThreadMessageSnapshot` は `role` と `content` しか持たない。このため、前回の回答で
明示的に不足していた観点を構造化データとして利用できず、LLM が回答本文から推測するしかない。

結果として、follow-up で次の判断材料が planner へ安定して渡らない。

- ユーザーが今回求めている回答形式・比較軸・深さ。
- thread 内ですでに説明した内容。
- 前回までの回答で満たせなかった観点のうち、今回の要求にも関係するもの。
- ユーザーが同じ thread で進めている調査・作業の流れ。

## Evidence

- `app/agent/question_resolution/contract.py` の `ResolvedQuestion` は
  `standalone_question` / `user_intent` / `prior_coverage` /
  `user_activity_context` を持ち、すでに単純な question rewrite より広い契約である。
- `app/agent/question_resolution/service.py` は history が空なら LLM を呼ばず、既知の
  resolver 失敗時は元質問を passthrough して run を継続する。
- `app/queue/tasks/agent_run.py` は context preparation 相当の処理を planner より前に
  実行し、結果を `AnswerQuestionInput` に詰めて agent core へ渡す。
- `app/agent/planning/ai/prompts.py` は `user_intent` / `prior_coverage` /
  `user_activity_context` を受け、retrieval mode と調査目的を決める。
- `app/models/agent_message.py` の assistant message は `missing_aspects` を JSONB array
  として保存する。user message は DB CHECK により常に空配列である。
- `app/agent/runs/result_mapper.py` は完成した `AnswerQuestionResult.missing_aspects` を
  assistant message へ保存する。
- `app/agent/threads/repository.py::read_recent_messages_before` は現在
  `AgentMessage.role` と `AgentMessage.content` だけを読むため、保存済み
  `missing_aspects` は worker の履歴窓へ届かない。
- `app/agent/answering/orchestration.py` は direct 経路に最新 assistant 回答本文の
  `previous_answer` を渡しており、「表にして」「短くして」等の変換ではすでに前回答を
  verbatim で再利用する。
- 過去回答の source は `agent_message_sources` に保存されているが、現在の agent input、
  planner、evidence collection には渡していない。

## Goal

planner とanswererの前にthread-scopedな質問コンテキストを準備し、後続工程が次を区別できる
状態にする。

1. 単独で意味が通る今回の質問。
2. 回答内容として今回必ず満たすこと。
3. 回答の形式・深さ・対象読者など、今回どのように答えるか。
4. 今回に関係する説明済みの内容。
5. ユーザーがこのthreadで現在達成しようとしている目的。

context preparation は会話状態を記述するまでを責務とし、その状態を満たすための
retrieval mode、検索文、外部調査目的、時間窓は planner が決定する。

## 用語と責務境界

```text
thread messages + current question
                │
                ▼
QuestionContextService
  - 今回の質問の意味を確定する
  - 内容要件と表現要件を分けて抽出する
  - 今回に関係する既回答を要約する
  - 過去不足・ユーザー訂正を今回の要件へ昇格する
  - thread のactive goalを抽出する
                │ QuestionContext
                ├── PlanningRequest(context, as_of) → QuestionPlanner
                └── AnsweringRequest(context, as_of) → direct / evidence answerer
```

context preparation が答える問い:

> ユーザーは今回何を答えてほしく、どのような形を求め、どの目的の中で質問しているか。

planner が答える問い:

> 未解決の要求を満たすため、今回どの情報をどの経路で取得するか。

保存済み `missing_aspects` と明示的feedbackはcontext preparationの入力材料であり、
`QuestionContext` の独立fieldにはしない。今回も扱うべきものだけをcontent/response requirementへ
昇格する。今回のretrievalとanswering後にも満たせなかったrequirementだけを最終結果の
`missing_aspects` とする。

## 設計判断

### 1. package と主要型を `question_context` へ改名する

```text
backend/app/agent/question_resolution/
    ↓
backend/app/agent/question_context/
```

主要シンボル:

| 現在 | 変更後 |
|---|---|
| `QuestionResolutionService` | `QuestionContextService` |
| `QuestionResolver` | `QuestionContextGenerator` |
| `GeminiQuestionResolver` | `GeminiQuestionContextGenerator` |
| `ResolvedQuestion` | `QuestionContext` |
| `ResolvedQuestionDraft` | `QuestionContextDraft` |
| `QuestionResolutionResponseInvalidError` | `QuestionContextResponseInvalidError` |
| `resolved_question_from_draft()` | `question_context_from_draft()` |
| serviceの `resolve()` | `prepare()` |
| generatorの `resolve()` | `generate()` |
| `build_question_resolver()` | `build_question_context_generator()` |

serviceはpackage内でcontextを扱うことが明らかなため短い `QuestionContextService` とし、
準備操作は `prepare()` で表す。provider adapter は context の完成責任を持たずlenient draftを
生成するため、`Resolver` ではなく `Generator` とする。

```python
class QuestionContextGenerator(Protocol):
    async def generate(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> QuestionContextDraft: ...
```

公開ライブイベント `question.resolved` は改名しない。このイベントは context 全体ではなく、
元質問と異なる `standalone_question` が生成された事実を表し、既存 API / frontend 契約でも
あるためである。

### 2. `QuestionContext` は今回の要望と関連文脈だけを持つ

```python
class QuestionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    standalone_question: StandaloneQuestion
    content_requirements: list[AnswerRequirement] = Field(
        default_factory=list,
        max_length=MAX_CONTENT_REQUIREMENTS,
    )
    response_requirements: list[AnswerRequirement] = Field(
        default_factory=list,
        max_length=MAX_RESPONSE_REQUIREMENTS,
    )
    relevant_prior_coverage: RelevantPriorCoverage = ""
    active_goal: ActiveGoal = ""
```

各フィールドの意味:

| フィールド | 意味 |
|---|---|
| `standalone_question` | 履歴なしでも意味が通る今回の質問。retrieval / fallback の正本 |
| `content_requirements` | 回答に含める対象・観点・比較軸・期間等、「何を答えるか」 |
| `response_requirements` | 形式・簡潔さ・深さ・対象読者等、「どのように答えるか」 |
| `relevant_prior_coverage` | 今回に関係する説明済み内容。真実認定ではなく会話上の既出情報 |
| `active_goal` | thread内でユーザーが現在達成しようとしている明示的な目的 |

`content_requirements` と `response_requirements` は別listにする。plannerは前者を調査漏れ防止の
正本として使い、後者は要求された深さが調査量に影響する場合だけ補助的に使う。answererは
両方を回答完成条件として使う。履歴に根拠がないcoverageやgoalを埋めることを要求しない。

### 3. 履歴snapshotへ保存済み `missing_aspects` を追加する

```python
@dataclass(frozen=True, slots=True)
class ThreadMessageSnapshot:
    role: Literal["user", "assistant"]
    content: str
    missing_aspects: tuple[str, ...] = ()
```

`AgentThreadRepository.read_recent_messages_before()` は既存の role / content に加えて
`AgentMessage.missing_aspects` を読む。

- user message は常に `missing_aspects=()` とする。
- assistant message は DB 値のうち非空文字列だけを順序維持で渡す。
- JSONB array に非文字列が存在しても context preparation 全体を落とさず、その要素だけを
  無視する。DB CHECK は array までしか保証せず、既存データを信頼境界の外として扱う。
- 履歴窓は既存どおり最大6メッセージ、本文は prompt 投入時に1件2,000文字へ cap する。
- `missing_aspects` も untrusted input とし、prompt 境界を越えて命令として解釈させない。

LLM へは各 assistant message の本文と同じ単位で、その回答に属する
`missing_aspects` を渡す。別メッセージの不足項目と混同できない形式にする。

prompt 用履歴を作るとき、`_history_for_prompt()` は `missing_aspects` を落とさず保持する。
全履歴窓を通じて順序維持で重複を除き、最大8件・各300文字へ正規化した値だけをpromptへ
入れる。保存済み不足はcontext generatorの入力材料であり、完成型へそのまま複製しない。

### 4. context generator は過去不足とfeedbackを今回の要件へ昇格する

Gemini structured output schemaは次を返す。

```python
class QuestionContextDraft(BaseModel):
    standalone_question: str
    content_requirements: list[str] = Field(default_factory=list)
    response_requirements: list[str] = Field(default_factory=list)
    relevant_prior_coverage: str = ""
    active_goal: str = ""
    explicit_feedback_detected: bool = False
```

prompt 規約:

- `standalone_question` は自己完結している質問を不要に書き換えない。
- `content_requirements` は対象・観点・比較軸・期間等、「何を答えるか」を分解する。
- `response_requirements` は形式・簡潔さ・深さ・対象読者等、「どう答えるか」を分解する。
- 保存済み `missing_aspects` のうち今回も扱うべき内容は、対応するrequirementへ昇格する。
- 「Intelが抜けている」はcontent requirement、「表にしてと言った」はresponse requirementへ
  反映し、生のfeedback本文を完成contextへ残さない。
- `relevant_prior_coverage` は今回に関係する既回答だけを簡潔にまとめる。
- `active_goal` はthread内またはcurrent questionに明確な根拠がある目的だけを記載する。
- 新topicでは古いcoverageとactive goalを空にし、前topicへ引っ張らない。
- `explicit_feedback_detected` は現在の質問が過去回答の不履行を明示した場合だけtrueにする。
- retrieval mode、検索query、検索provider、source再利用可否は出力しない。

LLM出力後、`question_context_from_draft()` はrequirementsをclean・dedup・capし、service側で
決定的にIDを採番する。保存済み不足とfeedbackは生成材料であり、完成型の独立fieldにはしない。

```python
class QuestionContextTelemetry(BaseModel):
    explicit_feedback_detected: bool = False
    previous_answer_had_missing_aspects: bool = False

class QuestionContextPreparationResult(BaseModel):
    context: QuestionContext
    telemetry: QuestionContextTelemetry
```

`explicit_feedback_detected` はLLM draftから完成する。`previous_answer_had_missing_aspects` は
LLMに生成させず、serviceが履歴窓の最新assistant `missing_aspects` から決定的に計算する。
telemetryは観測pipelineへ流す専用データであり、業務ロジックとしてplanner/answererへ渡さない。

### 5. service は毎run実行し、既知失敗ではpassthroughする

```python
class QuestionContextService:
    async def prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> QuestionContextPreparationResult:
        ...
```

constructorは `generator: QuestionContextGenerator | None` を受け取る。workerはhistoryの有無に
かかわらず `build_question_context_generator()` を試み、構築に失敗した場合は `None` を渡して
局所fallbackさせる。

- history の有無にかかわらずgeneratorを1回呼ぶ。
- history が空の場合もcurrent questionから回答要求を抽出するが、完成型の
  `standalone_question` は元質問へ決定的に固定する。LLMが返した書き換えは採用しない。
- history が空の場合、`relevant_prior_coverage` は空、telemetryの両fieldはfalseに固定する。
  current questionに明示されたrequirementsとactive goalは採用できる。
- AI provider error、response envelope不正、Pydantic validation errorでは run を落とさず、
  元質問、元質問から決定的に作る `c1` content requirement、空response requirements、
  空coverage/goalへfallbackする。telemetryは `explicit_feedback_detected=false` とし、
  `previous_answer_had_missing_aspects` は履歴から計算した値を保持する。
- generatorの構築失敗も同じ既知失敗として扱い、run全体を落とさない。
- fallback 時に全 `missing_aspects` を無条件で引き継がない。現在の質問との関連を判定できず、
  無関係な不足項目で planner を歪めるためである。
- 想定外例外は既存どおり明示列挙の外とし、握りつぶさない。
- log に question、history、context、missing_aspects、source 内容を載せない。

観測語彙:

```text
metric: vector.agent.question_context.outcome
attributes:
  result = prepared | failed
  explicit_feedback_detected = true | false
  previous_answer_had_missing_aspects = true | false
warning event: question_context_preparation_failed
```

旧 `vector.agent.question_resolution.outcome` は本 slice の切替と同時に emit を終了する。
dashboard / alert が存在する場合は実装前に参照先を確認し、同じ変更単位で更新する。旧metricとの
二重emitは行わない。

### 6. 共通contextをconsumer別requestへ包む

```python
class AnswerQuestionInput(BaseModel):
    context: QuestionContext
    as_of: datetime
    previous_answer: str = ""

class PlanningRequest(BaseModel):
    context: QuestionContext
    as_of: datetime

class AnsweringRequest(BaseModel):
    context: QuestionContext
    as_of: datetime
```

`QuestionContext` はユーザーが何を求めているかの共通正本である。`as_of` は会話文脈ではなく
実行メタデータなのでcontextへ入れず、consumer別requestに置く。

workerはagent core入口へ次を渡し、orchestratorがconsumer別requestを構築する。

```python
AnswerQuestionInput(
    context=prepared.context,
    as_of=as_of,
    previous_answer=latest_assistant_answer,
)

planning_request = PlanningRequest(context=input.context, as_of=input.as_of)
answering_request = AnsweringRequest(context=input.context, as_of=input.as_of)
```

`previous_answer` は今までどおり履歴窓の最新 assistant content を worker が決定的に取り出す。
context generatorによる要約や書き換えを通さず、direct answererだけへ別引数で渡す。

### 7. planner は context を実行戦略へ変換する

plannerは `PlanningRequest` を受け、`request.context` を実行戦略へ変換する。

planner の規約:

- `standalone_question` を調査対象の中心として扱う。
- `content_requirements` を今回満たすべき内容の正本とし、query/goalの抜けを防ぐ。
- `response_requirements` は原則answerer向けであり、表・文体・簡潔さだけを理由にretrievalを
  増やさない。要求された技術的深さ等が必要情報量に影響する場合だけ補助的に使う。
- `relevant_prior_coverage` は不要な繰り返しを避ける判断材料とする。ただし質問が再説明、比較、更新を
  明示的に求める場合は必要な範囲を再取得できる。
- `active_goal` は調査目的の方向付けに使い、履歴にない事実の根拠にしない。
- どの不足を今回の plan で扱うかは planner が決め、context preparation は retrieval mode や
  query を先回りして決めない。
- planner failure の `safe_fallback_plan` は引き続き
  `request.context.standalone_question` をfallback queryに使う。

plannerの `QuestionPlan` schemaは変更しない。contextはplanの入力であり、plan resultへ複製しない。

### 8. answererも同じ `QuestionContext` を受ける

direct/evidence answererは `AnsweringRequest` を受け、plannerと同じ `QuestionContext` を
回答の正本として使う。

```text
AnsweringRequest
  context.standalone_question
  context.content_requirements
  context.response_requirements
  context.relevant_prior_coverage
  context.active_goal
  as_of
```

- `content_requirements` は回答対象・観点の網羅性を決める。
- `response_requirements` は形式・深さ・対象読者等の回答方法を決める。
- `relevant_prior_coverage` は不要な繰り返しを避け、差分・更新要求を形作る。
- `active_goal` は回答の優先順位を目的へ合わせるが、事実根拠にはしない。

evidence answererには `AnsweringRequest` に加えて今回のevidenceとtarget time windowを渡す。
direct answererには `AnsweringRequest` に加えてverbatimな `previous_answer` を渡す。

保存済み不足やfeedback本文はanswererへ生のまま渡さない。今回必要な内容はすでにrequirementsへ
昇格されており、answererの未達自己申告もrequirement IDだけで行う。

### 9. 過去sourceの実再利用は別sliceにする

本 slice では `agent_message_sources` を context generator、planner、evidence collectorへ渡さない。
source再利用には少なくとも次の独立した契約が必要である。

- 「その根拠は？」と、最新性が必要な「その後どうなった？」の区別。
- external URL / evidence claim の鮮度と再検証方針。
- internal article 削除後に `analyzed_article_id=NULL` となるsnapshotの扱い。
- 過去messageの `source_ref` を新messageの引用markerへ再対応する方法。
- `planned_mode="none"` では sources を禁止する現在の provenance 契約との整合。

本 slice は将来の source再利用に必要な「何を求めているか」「何が未解決か」を先に安定させる。
前回答本文の `previous_answer` 再利用は既存どおり維持する。

### 10. context preparationとplannerは別contractのまま維持する

両方とも同じ軽量LLM providerを使う実装であっても、責務と失敗条件は統合しない。

- `QuestionContext` はplannerと両answererが共有するユーザー要望の正本である。
- `PlanningRequest` / `AnsweringRequest` はconsumerと実行メタデータを明示するwrapperであり、
  context解釈を複製しない。
- context generatorとplannerは初回を含む毎runで必要だが、異なるcontractとして維持する。
- context生成失敗は元質問でplannerへ進み、planner失敗はinternal retrievalへfallbackする。
- context preparationは存在する場合だけthread historyを使い、plannerはDBと履歴ロードを知らない。
- plannerのschema repair retryでcontext解釈まで再実行しない。

将来、実測したlatency / costが問題になった場合は、`QuestionContext` と `QuestionPlan` のcontractを
維持したままprovider callだけをまとめられるか別途評価する。本sliceでは計測根拠なしに統合しない。

## 実行フロー

```text
run_agent_answer
  → acquire run
  → read_recent_messages_before(
        role, content, missing_aspects,
        before_seq=current_user_message.seq,
        limit=6,
    )
  → QuestionContextService.prepare(...)
      - historyなし: generator実行 / standalone_questionは元質問へ固定
      - 成功: QuestionContextPreparationResult(context, telemetry)
      - 既知失敗: failed / passthrough
  → historyあり、かつstandalone_questionが元質問と異なる成功時だけ question.resolved event
  → AnswerQuestionInput(context, as_of, previous_answer)を構築
  → QuestionPlanner.plan(PlanningRequest(context, as_of))
      - contextをretrieval strategyへ変換
  → NoRetrievalPlan
      - direct answer(AnsweringRequest(context, as_of), previous_answer)
  → RetrievalPlan
      - evidence collection
      - evidence answer(AnsweringRequest(context, as_of), evidence, target_time_window)
      - 今回も未充足の項目をmissing_aspectsとして確定
  → completed resultをmessage + sourcesへ保存
```

## 変更対象

```text
backend/app/agent/question_resolution/              # package rename元
backend/app/agent/question_context/                 # rename先
backend/app/agent/threads/contracts.py              # snapshotにmissing_aspects追加
backend/app/agent/threads/repository.py             # JSONB列の読み込みと安全な投影
backend/app/agent/contract.py                       # QuestionContext / AnswerQuestionInput
backend/app/agent/composition.py                    # generator builder rename
backend/app/agent/planning/contract.py              # PlanningRequest
backend/app/agent/planning/ai/gemini.py             # context入力配布
backend/app/agent/planning/ai/gemini_prompt.py      # prompt renderer拡張
backend/app/agent/planning/ai/prompts.py            # planner規約追加
backend/app/agent/answering/contract.py              # AnsweringRequest
backend/app/queue/tasks/agent_run.py                 # prepare呼び出しとinput構築
backend/tests/agent/question_resolution/            # test package rename元
backend/tests/agent/question_context/               # rename先と新contract test
backend/tests/agent/threadsまたは既存repository test # missing_aspects投影
backend/tests/agent/planning/                        # planner prompt / input test
backend/tests/agent/answering/                       # 配布と未充足維持test
backend/tests/agent/test_agent_run_task.py           # worker統合test
```

既存仕様 `backend/specs/agent-conversation-context-slice.md` は実装時点の履歴として残し、
本仕様から後続の改名・拡張を定義する。過去仕様を現在名へ全面置換しない。

## API / DB / dependency

- REST endpoint、request、response shapeは変更しない。
- `question.resolved` のevent typeとpayloadは変更しない。
- frontend生成型の変更はないため `/gen-types` は不要。
- DB schemaは変更しない。既存 `agent_messages.missing_aspects` を読むためmigration不要。
- 新規dependencyは追加しない。
- 認証・認可、thread ownership、run ownershipは変更しない。

## Invariants

- context preparationは **thread-scoped**。他thread、他user、cross-thread memoryを読まない。
- agent coreはDBを知らない。worker / thread repositoryが履歴を読み、完成した
  `AnswerQuestionInput` だけをagent coreへ渡す。
- context preparationはユーザー要求と過去状態を記述し、retrieval strategyを決めない。
- plannerだけがretrieval mode、internal query、external research goal、time windowを決める。
- `QuestionContext` はplannerとanswererが共有するユーザー要望の正本である。
- `QuestionContextTelemetry` は観測専用であり、planner/answererの判断入力にしない。
- `content_requirements` と `response_requirements` は混ぜず、両方の未達IDを入力allowlistの
  部分集合として検証する。
- `relevant_prior_coverage` は過去回答の内容であり、現在も正しい事実として扱わない。
- `active_goal` はthread/current questionに明示的根拠がある場合だけ設定し、新topicでは空にする。
- 保存済み `missing_aspects` とfeedback本文はcontext生成材料であり、完成contextや
  planner/answererへ生のまま渡さない。
- 過去の未解決項目は今回の回答不能を意味しない。今回の `missing_aspects` はretrievalと
  synthesisの後に改めて確定する。
- `as_of` は `QuestionContext` に含めず、`PlanningRequest` / `AnsweringRequest` の
  実行メタデータとして渡す。
- 初回質問ではLLM出力にかかわらず `standalone_question` を元質問へ固定し、workerも
  historyなしでは `question.resolved` eventをemitしない。
- context preparationの既知失敗でrunを落とさず、元質問 + `c1` fallback content requirement +
  空response requirements/coverage/goalへ劣化する。
- context preparationのlog / metricに質問本文、履歴、回答断片、missing_aspectsを載せない。
- history、missing_aspects、生成contextは全LLM投入先でuntrusted inputとしてsanitizeする。
- `previous_answer` は最新assistant本文のverbatimであり、LLM context生成物へ置換しない。
- 事実の接地は今回のevidenceだけを正本とする。context単独を引用根拠にしない。
- direct経路は過去の不足事実を補完せず、現在のsourcesなし契約を維持する。

## Non-goals

- 過去 `AgentMessageSource` の再利用、引用markerの再採番、source freshness判定。
- plannerとcontext generatorの1回のLLM callへの統合。
- cross-thread長期memory、user profile、嗜好学習、personalization。
- requirement kindのenum taxonomy化。content/responseの2list以上には細分化しない。
- thread全履歴の動的要約、token budgetベースの履歴窓、context cache。
- `QuestionContext` のDB永続化やthread detail APIへの公開。
- `progress_stage` への `preparing_context` 追加。
- planner output schema、retrieval mode語彙、検索機構の変更。
- frontend表示文言・画面構成の変更。

## Tests

### Contract

1. `QuestionContextDraft` の文字列をstripし、各capを維持する。
2. content requirementsの空文字・重複・件数超過・文字数超過を決定的に正規化する。
3. response requirementsをcontentと混ぜず、独立して正規化する。
4. 空の `relevant_prior_coverage` / `active_goal` と、両fieldがfalseのtelemetryを正当値として
   受け入れる。

### Thread repository

5. recent historyがrole / content / missing_aspectsをseq昇順で返す。
6. user messageのmissing_aspectsは常に空tupleになる。
7. JSONB array中の非文字列・空文字を安全に除外する。
8. before_seq、limit、thread境界が既存どおり維持される。

### Context preparation service / Gemini adapter

9. historyなしでもgeneratorを呼びrequirements/明示active goalを採用しつつ、
   `standalone_question` / coverage / telemetryを決定値へ固定して `prepared` metricを記録する。
10. historyありの成功時に5つのcontext fieldとtelemetryを返し、`prepared` metricを記録する。
    `previous_answer_had_missing_aspects` は履歴中の直近assistantだけから決まり、それ以前の
    assistantに不足があっても直近が空ならfalseになる。
11. provider / response / validationの既知失敗でpassthroughし、質問本文を含まないwarningと
    `failed` metricを記録する。この場合も `previous_answer_had_missing_aspects` は履歴から
    決定的に維持する。
12. 想定外例外は握りつぶさない。
13. promptにmessage単位のmissing_aspectsが入り、境界タグを含んでもuntrusted blockを脱出しない。
14. Gemini schemaがcontent/response requirement arrays、coverage、active goal、feedback boolを
    必須fieldとして要求する。

### Planner / answering distribution

15. plannerが `PlanningRequest(context, as_of)` を受け取る。
16. planner promptで全context fieldがそれぞれsanitizeされる。
17. planner fallback queryが `request.context.standalone_question` になる。
18. direct/evidence answererがplannerと同じcontextを持つ `AnsweringRequest` を受ける。
19. `previous_answer` はcontextに入らず、direct経路へだけ別引数で渡る。
20. 保存済み不足とfeedback本文は生配布されず、今回扱うものだけがcontent/response
    requirementsを通じてanswererへ届く。

### Worker integration / compatibility

21. workerがmissing_aspects付きhistoryをprepareし、完成したcontextをagentへ渡す。
22. historyありの成功かつstandalone_question変更時だけ既存 `question.resolved` eventをemitする。
23. fallback / echo時にeventをemitしない。
24. 初回質問でLLMがstandalone_questionを書き換えても元質問を採用し、workerのhistory guardにより
    `question.resolved` eventをemitしない。
25. package rename後に実行コード・testから `app.agent.question_resolution` importが残らない。
26. API schemaとfrontend生成型に差分がない。

## Verification

実装時は次を行う。

1. `/check` でbackend lint / format / type / testを実行する。
2. `backend/tests/agent/question_context/`、thread repository、planning、answering、worker統合testを
   個別実行して失敗箇所を切り分ける。
3. `rg "question_resolution|QuestionResolution|ResolvedQuestion" backend/app backend/tests` で
   意図しない旧名が残っていないことを確認する。過去仕様内の記録は除外する。
4. OpenAPI生成差分がないことを確認する。Pydantic API schemaを変更していないため
   `/gen-types` は実行しない。
5. migration headに差分がなく、DB migrationが追加されていないことを確認する。
6. 本仕様のservice behavior、実行フロー、Tests 9・24がすべて「初回もgenerator実行・質問固定・
   eventなし」を表し、旧skip契約が残っていないことを確認する。

## Done

- `app/agent/question_context/` が「planner/answerer前の質問コンテキスト準備」という責務を名前と
  contractの両方で表している。
- `QuestionContextService.prepare()` が元質問とthread履歴から
  `QuestionContextPreparationResult` を返す。
- contextに `standalone_question` / `content_requirements` / `response_requirements` /
  `relevant_prior_coverage` / `active_goal` が含まれる。
- 保存済みassistant `missing_aspects` と明示feedbackが履歴入力として届き、今回に関係する値だけが
  content/response requirementsへ昇格する。
- plannerが完成したcontextを受けてretrieval strategyを決定し、context preparationとの
  責務が重複していない。
- plannerとanswererがconsumer別requestを通じて同じ `QuestionContext` を受ける。
- `as_of`、evidence、previous answer、telemetryを `QuestionContext` に混入させない。
- 初回は追加LLM callを行うが質問本文を決定的に維持し、context生成失敗、planner fallback、
  direct回答、evidence回答の劣化特性とprovenance契約が維持される。
- REST / frontend / DB schema / dependencyに変更がない。
- source実再利用は本sliceに混入せず、別途必要な契約がNon-goalsに明記されている。
- 指定testと `/check` がgreenになる。
