# Agent question context preparation slice 仕様

## 位置付け

前提仕様: `backend/specs/agent-conversation-context-slice.md`。

既存の `app/agent/question_resolution/` は、thread の直近履歴から
`standalone_question` / `user_intent` / `prior_coverage` /
`user_activity_context` を生成し、planner と回答生成へ渡している。本 slice はこの処理を
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

planner の前に thread-scoped な質問コンテキストを準備し、後続工程が次を区別できる
状態にする。

1. ユーザーが今回求めていること。
2. これまでに説明済みのこと。
3. 前回までに未解決だったことのうち、今回も考慮すべきこと。
4. ユーザーが thread 内で進めている調査・作業の流れ。
5. 上記を踏まえ、単独で意味が通る今回の質問。

context preparation は会話状態を記述するまでを責務とし、その状態を満たすための
retrieval mode、検索文、外部調査目的、時間窓は planner が決定する。

## 用語と責務境界

```text
thread messages + current question
                │
                ▼
QuestionContextService
  - 今回の質問の意味を確定する
  - 回答要求を抽出する
  - 既回答を要約する
  - 関係する過去の未解決項目を選ぶ
  - thread 内の活動文脈を抽出する
                │ QuestionContext
                ▼
QuestionPlanner
  - retrieval mode を選ぶ
  - internal queries を作る
  - external research goals を作る
  - target time window を決める
                │ QuestionPlan
                ▼
retrieval / direct answer / evidence answer
```

context preparation が答える問い:

> ユーザーは今回何を求めていて、これまで何が説明済みで、何がまだ未解決か。

planner が答える問い:

> 未解決の要求を満たすため、今回どの情報をどの経路で取得するか。

「過去に未解決」と「今回も回答不能」は区別する。planner 前に確定できるのは
`prior_unresolved_aspects` までであり、今回の retrieval 後にも満たせなかった内容だけを
最終結果の `missing_aspects` とする。

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

### 2. `QuestionContext` は既存4フィールドと過去の未解決項目を持つ

```python
class QuestionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    standalone_question: StandaloneQuestion
    user_intent: UserIntent = ""
    prior_coverage: PriorCoverage = ""
    prior_unresolved_aspects: list[PriorUnresolvedAspect] = Field(
        default_factory=list,
        max_length=8,
    )
    user_activity_context: UserActivityContext = ""
```

各フィールドの意味:

| フィールド | 意味 |
|---|---|
| `standalone_question` | 履歴なしでも意味が通る今回の質問。retrieval / fallback の正本 |
| `user_intent` | 回答形式、比較、深さ、対象読者など今回の回答要求 |
| `prior_coverage` | thread 内ですでに回答した内容。真実認定ではなく「過去に述べた内容」 |
| `prior_unresolved_aspects` | 保存済み `missing_aspects` のうち今回の要求にも関係する未解決項目 |
| `user_activity_context` | thread 内に明示的根拠がある調査・作業の流れ |

`prior_unresolved_aspects` は最大8件、各要素は strip 後1〜300文字とする。draft から完成型へ
変換するときに空文字除去、順序維持の重複排除、件数・文字数 cap を決定的に適用する。

既存4フィールドの文字数 cap と空文字許容は維持する。履歴に根拠がない goal、intent、
activity を埋めることを要求しない。

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
入れる。この正規化済みlistを、完成型で許可する未解決項目のallowlistにも使う。

### 4. context generator は未解決項目を「今回との関連」で選ぶ

Gemini structured output schema に `prior_unresolved_aspects: string[]` を追加する。

prompt 規約:

- `standalone_question` は自己完結している質問を不要に書き換えない。
- `user_intent` は今回の回答方法に関する要求だけを記載する。
- `prior_coverage` は今回に関係する既回答だけを簡潔にまとめる。
- `prior_unresolved_aspects` は履歴snapshotに構造化されている `missing_aspects` のうち、
  現在の質問または継続中の user goal に関係するものだけを選ぶ。
- 履歴に存在しない未解決項目を新しく作らない。
- 過去の `missing_aspects` が現在の質問と無関係なら空配列にする。
- `user_activity_context` は thread 内に明確な根拠がある場合だけ記載する。
- retrieval mode、検索query、検索provider、source再利用可否は出力しない。

LLM 出力後、`question_context_from_draft()` は `prior_unresolved_aspects` がpromptへ渡した
allowlistの部分集合であることを検証する。

```python
def question_context_from_draft(
    draft: QuestionContextDraft,
    *,
    allowed_unresolved_aspects: tuple[str, ...],
) -> QuestionContext:
    ...
```

LLM が新しい文字列を生成した場合、その要素は採用しない。表記ゆれを許容するための意味的一致は
行わず、strip 後の完全一致だけを採用する。

これにより、過去の未解決項目は LLM が自由生成する要約ではなく、DB に保存された値を
選択する構造になる。

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
    ) -> QuestionContext:
        ...
```

constructorは `generator: QuestionContextGenerator | None` を受け取る。workerはhistoryの有無に
かかわらず `build_question_context_generator()` を試み、構築に失敗した場合は `None` を渡して
局所fallbackさせる。

- history の有無にかかわらずgeneratorを1回呼ぶ。
- history が空の場合もcurrent questionから回答要求を抽出するが、完成型の
  `standalone_question` は元質問へ決定的に固定する。LLMが返した書き換えは採用しない。
- history が空の場合、`prior_coverage` / `prior_unresolved_aspects` /
  `prior_response_feedback`（後続仕様で追加）は空に固定する。current questionに明示された
  `user_intent` / `user_activity_context` とrequirementsは採用できる。
- AI provider error、response envelope不正、Pydantic validation errorでは run を落とさず、
  元質問とbase optional fieldが空のcontextを返す。後続仕様の `answer_requirements` だけは
  元質問から決定的に作る `r1` fallbackを持つ。
- generatorの構築失敗も同じ既知失敗として扱い、run全体を落とさない。
- fallback 時に全 `missing_aspects` を無条件で引き継がない。現在の質問との関連を判定できず、
  無関係な不足項目で planner を歪めるためである。
- 想定外例外は既存どおり明示列挙の外とし、握りつぶさない。
- log に question、history、context、missing_aspects、source 内容を載せない。

観測語彙:

```text
metric: vector.agent.question_context.outcome
result: prepared | failed
warning event: question_context_preparation_failed
```

旧 `vector.agent.question_resolution.outcome` は本 slice の切替と同時に emit を終了する。
dashboard / alert が存在する場合は実装前に参照先を確認し、同じ変更単位で更新する。旧metricとの
二重emitは行わない。

### 6. `AnswerQuestionInput` へ未解決項目を追加する

```python
class AnswerQuestionInput(BaseModel):
    question: str
    as_of: datetime
    user_intent: str = ""
    prior_coverage: str = ""
    prior_unresolved_aspects: list[str] = Field(default_factory=list)
    user_activity_context: str = ""
    previous_answer: str = ""
```

worker は `QuestionContext` を次のように配布する。

```python
AnswerQuestionInput(
    question=context.standalone_question,
    as_of=as_of,
    user_intent=context.user_intent,
    prior_coverage=context.prior_coverage,
    prior_unresolved_aspects=context.prior_unresolved_aspects,
    user_activity_context=context.user_activity_context,
    previous_answer=latest_assistant_answer,
)
```

`previous_answer` は今までどおり履歴窓の最新 assistant content を worker が決定的に取り出す。
context generator による要約や書き換えを通さない。

### 7. planner は context を実行戦略へ変換する

planner prompt に `prior_unresolved_aspects` を untrusted な構造化入力として追加する。

planner の規約:

- `standalone_question` と `user_intent` を今回の正の要求として扱う。
- `prior_coverage` は不要な繰り返しを避ける判断材料とする。ただし質問が再説明、比較、更新を
  明示的に求める場合は必要な範囲を再取得できる。
- `prior_unresolved_aspects` は今回解消を試みるべき既知の不足として扱う。
- 後続のanswer requirement tracking実装後は、`answer_requirements` だけを今回の達成判定の
  正本とする。`prior_unresolved_aspects` は過去不足を説明する履歴文脈であり、同じ内容を
  別の義務として追加しない。
- `user_activity_context` は調査目的の方向付けに使い、履歴にない事実の根拠にしない。
- どの不足を今回の plan で扱うかは planner が決め、context preparation は retrieval mode や
  query を先回りして決めない。
- planner failure の `safe_fallback_plan` は引き続き
  `input.question`（解決済み `standalone_question`）を fallback query に使う。

planner の `QuestionPlan` schemaは変更しない。`prior_unresolved_aspects` は plan の入力であり、
plan result へ複製しない。

### 8. 未解決項目はanswererへ直接渡さない

`prior_unresolved_aspects` はplannerまでの履歴文脈とし、evidence/direct answererへ生のまま
渡さない。後続のanswer requirement trackingで、今回も扱うべき過去不足だけを
`answer_requirements` へ昇格させる。

retrieval plan を実行した後、evidence answererへ以下を渡す。

```text
question
evidence
user_intent
prior_coverage
user_activity_context
answer_requirements  # 後続仕様で追加する今回の達成判定の正本
```

requirementsへ昇格されなかった `prior_unresolved_aspects` は今回の達成判定対象ではない。
answererの未達自己申告はrequirement IDだけで行い、過去不足と今回の義務を二重に評価しない。

direct answererには `prior_unresolved_aspects` を渡さない。direct は検索不要な変換・言い換えを
担当し、前回答本文の `previous_answer` を正本とする。過去に不足した事実を根拠なしで補完させない。

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

- contextはplannerだけでなくevidence answererとdirect経路の入力準備にも使う。
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
      - 成功: QuestionContext
      - 既知失敗: failed / passthrough
  → historyあり、かつstandalone_questionが元質問と異なる成功時だけ question.resolved event
  → AnswerQuestionInputを構築
  → QuestionPlanner.plan(input)
      - contextをretrieval strategyへ変換
  → NoRetrievalPlan
      - direct answer(previous_answerを利用)
  → RetrievalPlan
      - evidence collection
      - evidence answer(
          prior_coverage,
          user_activity_context,
          answer_requirements,
        )
      - 今回も未充足の項目をmissing_aspectsとして確定
  → completed resultをmessage + sourcesへ保存
```

## 変更対象

```text
backend/app/agent/question_resolution/              # package rename元
backend/app/agent/question_context/                 # rename先
backend/app/agent/threads/contracts.py              # snapshotにmissing_aspects追加
backend/app/agent/threads/repository.py             # JSONB列の読み込みと安全な投影
backend/app/agent/contract.py                       # AnswerQuestionInput拡張
backend/app/agent/composition.py                    # generator builder rename
backend/app/agent/planning/ai/gemini.py             # context入力配布
backend/app/agent/planning/ai/gemini_prompt.py      # prompt renderer拡張
backend/app/agent/planning/ai/prompts.py            # planner規約追加
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
- `prior_coverage` は過去回答の内容であり、現在も正しい事実として扱わない。
- `prior_unresolved_aspects` は保存済み `missing_aspects` の部分集合とし、新しい不足項目を
  context generatorに創作させない。
- 過去の未解決項目は今回の回答不能を意味しない。今回の `missing_aspects` はretrievalと
  synthesisの後に改めて確定する。
- 初回質問ではLLM出力にかかわらず `standalone_question` を元質問へ固定し、workerも
  historyなしでは `question.resolved` eventをemitしない。
- context preparationの既知失敗でrunを落とさず、元質問 + base optional field空 + 後続仕様の
  `r1` fallback requirementへ劣化する。
- context preparationのlog / metricに質問本文、履歴、回答断片、missing_aspectsを載せない。
- history、missing_aspects、生成contextは全LLM投入先でuntrusted inputとしてsanitizeする。
- `previous_answer` は最新assistant本文のverbatimであり、LLM context生成物へ置換しない。
- 事実の接地は今回のevidenceだけを正本とする。context単独を引用根拠にしない。
- direct経路は過去の不足事実を補完せず、現在のsourcesなし契約を維持する。

## Non-goals

- 過去 `AgentMessageSource` の再利用、引用markerの再採番、source freshness判定。
- plannerとcontext generatorの1回のLLM callへの統合。
- cross-thread長期memory、user profile、嗜好学習、personalization。
- `user_intent` taxonomyのenum化。
- thread全履歴の動的要約、token budgetベースの履歴窓、context cache。
- `QuestionContext` のDB永続化やthread detail APIへの公開。
- `progress_stage` への `preparing_context` 追加。
- planner output schema、retrieval mode語彙、検索機構の変更。
- frontend表示文言・画面構成の変更。

## Tests

### Contract

1. `QuestionContextDraft` の全文字列をstripし、既存capを維持する。
2. `prior_unresolved_aspects` の空文字・重複・件数超過・文字数超過を決定的に正規化する。
3. `prior_unresolved_aspects` は入力historyに存在する `missing_aspects` の部分集合だけを採用する。
4. 空のoptional contextと空の未解決項目listを正当値として受け入れる。

### Thread repository

5. recent historyがrole / content / missing_aspectsをseq昇順で返す。
6. user messageのmissing_aspectsは常に空tupleになる。
7. JSONB array中の非文字列・空文字を安全に除外する。
8. before_seq、limit、thread境界が既存どおり維持される。

### Context preparation service / Gemini adapter

9. historyなしでもgeneratorを呼び、回答要求の抽出結果を採用しつつ
   `standalone_question` / prior系fieldを決定値へ固定し、`prepared` metricを記録する。
10. historyありの成功時にbase 5フィールドをcleanし、後続仕様のrequirements/feedbackを含む
    完成contextを返して `prepared` metricを記録する。
11. provider / response / validationの既知失敗でpassthroughし、質問本文を含まないwarningと
    `failed` metricを記録する。
12. 想定外例外は握りつぶさない。
13. promptにmessage単位のmissing_aspectsが入り、境界タグを含んでもuntrusted blockを脱出しない。
14. Gemini schemaが `prior_unresolved_aspects: ARRAY<STRING>` を必須fieldとして要求する。

### Planner / answering distribution

15. plannerが `prior_unresolved_aspects` を含む `AnswerQuestionInput` を受け取る。
16. planner promptで全context fieldがそれぞれsanitizeされる。
17. planner fallback queryが引き続きresolved `input.question`になる。
18. evidence経路でanswererへ生の `prior_unresolved_aspects` が渡らない。
19. direct経路へも `prior_unresolved_aspects` は渡らず、`previous_answer` は従来どおり渡る。
20. 今回扱う過去不足は後続仕様の `answer_requirements` だけを通じてanswererへ届く。

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

- `app/agent/question_context/` が「planner前の質問コンテキスト準備」という責務を名前と
  contractの両方で表している。
- `QuestionContextService.prepare()` が元質問とthread履歴から
  `QuestionContext` を返す。
- contextに `standalone_question` / `user_intent` / `prior_coverage` /
  `prior_unresolved_aspects` / `user_activity_context` と、後続仕様の
  `answer_requirements` / `prior_response_feedback` が含まれる。
- 保存済みassistant `missing_aspects` が履歴snapshotへ構造的に届き、今回に関係する値だけが
  `prior_unresolved_aspects` として採用される。
- plannerが完成したcontextを受けてretrieval strategyを決定し、context preparationとの
  責務が重複していない。
- 今回扱う過去不足だけが後続仕様の `answer_requirements` へ昇格し、answererへ生の
  `prior_unresolved_aspects` を二重配布しない。
- 初回は追加LLM callを行うが質問本文を決定的に維持し、context生成失敗、planner fallback、
  direct回答、evidence回答の劣化特性とprovenance契約が維持される。
- REST / frontend / DB schema / dependencyに変更がない。
- source実再利用は本sliceに混入せず、別途必要な契約がNon-goalsに明記されている。
- 指定testと `/check` がgreenになる。
