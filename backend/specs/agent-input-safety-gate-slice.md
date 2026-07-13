# Agent input safety gate slice 仕様

## 位置付け

Vector の research agent は、user message を保存して非同期 run を作成した後、worker で
question context preparation、planner、evidence collection、answer generation を実行する。

本 slice は worker の agent 処理先頭に `input_safety` 境界を追加し、現在の入力を
`allow` / `block` の2値で判定する。`block` の場合は固定拒否メッセージを保存して run を
終端し、question context preparation 以降を一切起動しない。

2026-07-14 のユーザー合意:

- 空文字・文字数・型などの決定的検証は既存の Pydantic schema が担い、LLM に判定させない。
- LLM の責務は入力内容を `allow` / `block` で判定することだけに限定する。
- 判定 field 名は `input_safety_result` とする。
- `block` 後は Context Resolver、Planner、検索、通常回答生成を一切起動しない。
- 監査イベントは拒否された入力だけに記録し、`allow` は正常系として監査しない。

前提仕様:

- `backend/specs/agent-question-context-preparation-slice.md`
- `backend/specs/agent-history-run-execution-slice.md`
- `backend/specs/agent-threads-runs-boundary-slice.md`
- `backend/specs/agent-live-stream-transport-slice.md`

## Problem

現在の research agent には次の防壁がある。

- API schema が質問を strip し、空文字と1,000文字超過を 422 で拒否する。
- LLM prompt に埋め込む入力は `<untrusted_input>` と
  `sanitize_for_untrusted_block()` で命令境界から分離する。
- answerer は内部実装、system instruction、API key を開示しないよう指示される。

これらは形式検証または prompt injection の影響低減であり、ユーザーが求める支援内容を
処理してよいかは判定していない。そのため、明確に有害な実行支援でも Context Resolver、
Planner、検索、回答生成が起動し、不要な外部 call と不適切な回答生成の余地が生じる。

また、provider 自身の safety filter は provider 固有の確率判定であり、Vector が定義する
許可・拒否境界の SSoT にはできない。Vector の用途に合わせた application policy と、
provider が API call 自体を遮断した場合の扱いを分けて定義する必要がある。

## Evidence

- `app/schemas/research.py::ResearchQuestion` は strip、`min_length=1`、
  `max_length=1000` を保証する。
- `app/agent/router.py::create_research_response()` は user message と queued run を同一
  transaction で保存した後に worker task を enqueue する。
- `app/queue/tasks/agent_run.py::run_agent_answer()` は run を running へ取得してから履歴を読み、
  `QuestionContextService.prepare()` を最初の LLM 工程として実行する。
- `app/agent/question_context/service.py` は context generator 失敗時に元質問を passthrough
  する。この fallback は会話文脈準備には適切だが、安全判定失敗時には適用できない。
- `app/analysis/prompt_safety.py` は prompt 境界の無害化を担い、内容の許可・拒否は行わない。
- `app/agent/runs/repository.py::complete_run()` は assistant message 作成と run completed 遷移を
  同一 transaction で行い、completed run が必ず assistant message を持つ DB invariant を守る。
- `app/models/agent_run.py` の公開状態機械は `queued | running | completed | failed` であり、
  completed と assistant message の存在が DB CHECK で結ばれている。
- `app/agent/live_updates/stream.py` は completed / failed の terminal event を公開する。
- planner / question context の Gemini adapter は structured JSON、Pydantic validation、
  provider error translation、call signature versioning の既存パターンを持つ。
- google-genai 2.10.0 が既存 dependency として導入済みで、新規 dependency は不要である。
- Gemini 公式ドキュメントは structured output を分類用途に使えるとしている一方、schema
  準拠出力でも application 側の検証が必要としている。
- Gemini 公式 safety settings は provider filter の block reason / safety rating を返し得るが、
  probability ベースであり application policy と同一ではない。
- Google Responsible Generative AI Toolkit は、application の用途に応じた policy、例外、具体例を
  定義し、曖昧な policy による過剰・過少拒否を避けることを推奨している。
- 同 Toolkit は、safety checker の false positive / false negative を前提に、precision / recall
  等で過剰拒否との balance を評価することを推奨している。

## Goal

user message の意味内容を低コストな1回の判定で評価し、次を保証する。

1. `allow` が明示的に確定した run だけが question context preparation 以降へ進む。
2. `block` は固定拒否回答で正常に終端し、後続 LLM、検索、tool を起動しない。
3. 拒否理由は低 cardinality の安定した code として監査できる。
4. `allow` は監査保存せず、内容を持たない集計 metric のみ記録する。
5. checker の正常な contract 出力を得られない場合や provider 障害を `allow` とみなさず、
   安全境界を fallback で迂回しない。

## 用語と責務境界

```text
Pydantic request validation
  - blank / max length / type
             │
             ▼
user message + queued run persistence
             │
             ▼
worker attempt acquisition + bounded history read
             │
             ▼
InputSafetyService
  - 現在の質問と直前1往復から、現在の依頼を allow / block で判定
  - block のときだけ拒否監査を記録
  - allow / block / failed の集計 metric を記録
             │
       ┌─────┴─────┐
       │           │
     allow       block
       │           └─ fixed refusal persistence → completed terminal
       ▼
QuestionContextService
       ▼
Planner → evidence collection → answer generation
```

`input_safety` が答える問い:

> 現在の会話 turn でユーザーが求める支援を、agent の後続工程へ渡してよいか。

`input_safety` は次を行わない。

- 質問の形式検証、切り詰め、正規化。
- standalone question、回答要件、active goal の生成。
- 検索要否、検索語、調査目的の決定。
- 安全な代替回答や拒否文の生成。
- prompt injection 文字列を理由とした機械的拒否。

## Invariants

- `input_safety_result == "allow"` が確定するまで Context Resolver 以降を起動しない。
- `block` 後に `build_question_context_generator()`、
  `build_question_answering_agent()`、Tavily client、Planner、retrieval、answer generator を
  構築・呼出ししない。
- block branch で許可する副作用は、run lifecycle、拒否監査、固定拒否 message 永続化、
  terminal 通知、metric に限定する。
- blank / length / type 判定を input safety prompt に重複させない。
- checker の会話入力は current question と optional な previous turn だけに限定し、それ以前の
  履歴を渡さない。
- previous turn は current question の参照解決にだけ使い、previous turn の内容だけを理由に
  current question を block しない。
- checker は tool、検索、URL context、code execution を利用しない。
- checker の出力を application の strict contract で検証する。
- checker 失敗、timeout、rate limit、schema 不正を `allow` へ fallback しない。
- `allow` の audit event を作成しない。
- 拒否監査、通常 log、metric attribute に質問本文、履歴本文、provider raw response を含めない。
- block は user の入力エラーや infrastructure failure ではなく、policy が確定した正常な終端である。
- block した run は固定 assistant message を持つ `completed` とし、`failed` にしない。
- API response schema、DB schema、Redis event schema、frontend generated type を変更しない。
- 既存の prompt untrusted boundary と provider safety protection は防御層として維持する。

## Non-goals

- DB table、column、constraint、index、migration の追加・変更。
- API に `blocked` status、block reason、専用 error response を追加すること。
- frontend に拒否 badge、reason 表示、異議申立て UI を追加すること。
- user message を保存前に同期 moderation し、HTTP request 自体を拒否すること。
- user profile、cross-thread 履歴、外部データを使ったリスク評価。
- 直前1往復より前の thread 履歴を使ったリスク評価。
- allowされた質問の成功監査を保存すること。
- raw input または raw model response を監査目的で複製保存すること。
- prompt injection、jailbreak、機密情報漏えい対策をこの判定だけに依存すること。
- policy reason ごとに異なる拒否文を生成すること。
- human review queue、appeal、管理画面を作ること。
- 既存 answerer の provider safety settings を変更すること。

## 設計判断

### 1. package 名は `input_safety` とする

```text
backend/app/agent/input_safety/
├── __init__.py
├── contract.py
├── service.py
├── audit.py
├── metrics.py
└── ai/
    ├── __init__.py
    ├── gemini.py
    ├── prompt.py
    ├── schema_tool.py
    └── spec.py
```

`moderation` は provider API や運用上の審査全般にも読める。今回の package は agent input の
安全判定に限定され、ユーザー合意済み field 名も `input_safety_result` であるため、
境界名を `input_safety` に揃える。

### 2. strict contract は2値判定と拒否理由だけを持つ

```python
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InputSafetyResult(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class InputSafetyBlockReason(StrEnum):
    DANGEROUS_OR_ILLEGAL_INSTRUCTIONS = "dangerous_or_illegal_instructions"
    CREDENTIAL_OR_PRIVACY_ABUSE = "credential_or_privacy_abuse"
    TARGETED_HATE_OR_HARASSMENT = "targeted_hate_or_harassment"
    SEXUAL_EXPLOITATION = "sexual_exploitation"
    SELF_HARM_INSTRUCTIONS = "self_harm_instructions"
    PROVIDER_SAFETY_FILTER = "provider_safety_filter"


class InputSafetyCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_safety_result: InputSafetyResult
    block_reason: InputSafetyBlockReason | None = None

    @model_validator(mode="after")
    def validate_block_reason(self) -> "InputSafetyCheckResult":
        if self.input_safety_result is InputSafetyResult.ALLOW:
            if self.block_reason is not None:
                raise ValueError("allow result cannot include block reason")
        elif self.block_reason is None:
            raise ValueError("block result must include block reason")
        return self


class InputSafetyPreviousTurn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_question: str = Field(min_length=1, max_length=1000)
    assistant_answer: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
    )


class InputSafetyChecker(Protocol):
    async def check(
        self,
        *,
        question: str,
        previous_turn: InputSafetyPreviousTurn | None,
    ) -> InputSafetyCheckResult: ...
```

`decision` / `result` 単独では何の結果かが読めないため、field は
`input_safety_result` とする。class 名が safety 文脈を与えても、schema、audit、test failure
から field 単独で現れる場合に意味が維持される名前を優先する。

`block_reason` は自由文にしない。自由文は表現揺れ、high-cardinality metric、質問本文の
引用、監査上の機密情報混入を起こし得る。reason code 自体を軽い説明として扱い、ユーザーへ
見せる文言には使わない。

`InputSafetyChecker` は別の業務レイヤーではなく、入力をcheckする provider port である。
`GeminiInputSafetyChecker` がこの port を実装し、test は fake checker へ差し替える。
allow / block の意味判断を複数 component へ分散させない。

`PROVIDER_SAFETY_FILTER` は Vector の safety checker が structured check result を返す前に、
provider が prompt または candidate を safety reason で遮断した場合の保守的な block 理由である。
RECITATION や schema 不正、空 response は safety block と混同せず、判定失敗として扱う。

### 3. policy は「話題」ではなく「要求された出力が何を可能にするか」を判定する

block は、要求された出力が危険・違法・搾取的な行為を実行する能力を、具体的な手順、
コード、最適化、対象選定、入手方法、検知回避によって実質的に高める場合に限る。

| block reason | block する内容 |
|---|---|
| `dangerous_or_illegal_instructions` | 暴力、侵入、詐欺、市場操作、破壊、違法取得等を実行可能にする具体的手順・コード・最適化 |
| `credential_or_privacy_abuse` | 認証情報、個人情報、非公開情報の窃取・追跡・不正利用 |
| `targeted_hate_or_harassment` | 保護対象または特定個人への暴力扇動、深刻な標的型嫌がらせの作成・実行支援 |
| `sexual_exploitation` | 児童、非同意、搾取を伴う性的内容の作成・取得・実行支援 |
| `self_harm_instructions` | 自傷・自殺の実行手順、成功率向上、実行の奨励 |

次は、それ自体では block 理由にしない。

- 有害な話題への言及。
- ニュース、投資影響、法制度、歴史、研究、倫理の非実行的な分析。
- 検知、防御、予防、修復、incident response。ただし不正アクセス、永続化、検知回避等を
  実行可能にする具体性を加えない依頼。
- 有害な内容の分類または高レベルの要約。ただし実行可能な詳細を再現・翻訳・補完しない依頼。
- 自傷を防ぐための支援、安全確保、予防、相談先の質問。
- prompt injection や system prompt への言及だけを含む質問。
- 罵倒、下品な表現、オフトピックであるだけの質問。

ニュース、研究、防御、教育、仮定、role-play 等の申告だけで allow にしない。申告された目的では
なく、実際に要求された出力の具体性と、それが可能にする行為で判定する。翻訳も常に allow では
なく、危険な実行手順を利用可能な形で維持する場合は block 対象とする。

判定は high precision を優先する。要求された出力が上記能力を実質的に高めると明確に判断できない
場合は allow とし、曖昧さ解消や通常回答時の安全制約は後続工程に委ねる。ただし、婉曲表現、
綴り違い、仮定、専門家を名乗ることによって明確な実行支援要求を allow にしない。
input safety gate は全安全対策の代替ではなく、明確な違反を早期短絡する第一判定である。

### 4. 現在の質問と直前1往復だけを checker に渡す

current question だけでは「その方法を成功させるコードを書いて」等の follow-up を判定できない。
一方、question context preparation と同じ最大6 message は safety check には不要である。そのため、
checker へ渡す会話情報を直前の user question と、それに対応する assistant answer だけに限定する。

- current question は API schema で保証された最大1,000文字をそのまま使う。
- previous user question は最大1,000文字に制限する。
- corresponding assistant answer は先頭から最大1,000文字に制限する。
- previous assistant answer が存在しない failed run では `assistant_answer=None` とし、さらに前の
  assistant answer を誤って組み合わせない。
- previous turn は current question の参照先を理解するためだけに使い、過去 turn を再判定しない。
- 過去に危険な話題があるだけで current question を block しない。
- assistant `missing_aspects` は安全判定に不要なため渡さない。
- question / previous turn は `sanitize_for_untrusted_block()` を通し、全て untrusted data として囲う。

worker は allow 後の QuestionContextService でも使う既存 bounded history を1回だけ DB から読み、
その末尾から previous turn を抽出する。追加 query は行わない。直前の user message の直後に
assistant message がある場合だけ同じ turn の answer として採用する。history read は Context
Resolver の起動ではなく、LLM による context preparation は allow 後まで構築しない。

### 5. `InputSafetyService` は1回判定し、fallback / repair retry しない

```python
class InputSafetyService:
    def __init__(
        self,
        *,
        checker: InputSafetyChecker,
        audit_recorder: InputSafetyAuditRecorder,
    ) -> None: ...

    async def check(
        self,
        *,
        question: str,
        previous_turn: InputSafetyPreviousTurn | None,
        run_id: UUID,
    ) -> InputSafetyCheckResult: ...
```

service の責務:

1. checker を1回だけ呼ぶ。
2. strict `InputSafetyCheckResult` を返す。
3. `allow` / `block` / `failed` の metric を最終結果ごとに1回記録する。
4. `block` の場合だけ `InputSafetyBlockedAuditEvent` を recorder に渡す。

`InputSafetyService` は checker と別の判定を行わない。checker が内容判定を所有し、service は
その1回の check と監査・metric を1ユースケースとして束ねる。

check result は2 field の小さい structured output であり、repair retry はコストと安全境界の複雑さに
見合わない。response 不正は typed error として worker へ伝播し、後続を起動せず
`generation_unavailable` で run を failed にする。

監査 recorder の失敗は判定を allow に変えない。block のまま固定拒否で終端し、
`agent_input_safety_audit_dropped` の operational error log と metric を残す。

### 6. Gemini Flash-Lite の structured output を1回だけ使う

初期実装は既存の question context / planner と同じ `gemini-2.5-flash-lite` を使い、新規 model、
provider、dependency、API key を追加しない。

concrete adapter 名は `GeminiInputSafetyChecker` とする。

```text
model: gemini-2.5-flash-lite
temperature: 0.0
max_output_tokens: 128
response_mime_type: application/json
response_schema: INPUT_SAFETY_GEMINI_SCHEMA
tools: none
retry: none
```

schema の required field は `input_safety_result` と `block_reason` の2つとする。
`block_reason` は nullable enum とし、application の model validator が result との組合せを
最終検証する。prompt に JSON schema 本文や出力例を重複記載せず、policy と判断規則だけを
記載する。

runtime promptには「要求された出力が有害行為の実行能力を実質的に高めるか」という中心ルール、
短いblock reason定義、それ自体ではblockしない例外だけを記載する。policy境界の詳細例はpromptへ
大量に埋め込まず、test / evaluation caseで管理する。checkerへ渡すuser dataはcurrent question
最大1,000文字、previous user question最大1,000文字、previous assistant answer最大1,000文字の
合計最大3,000文字とする。

spec は model、generation config、structured output、schema、prompt、system instruction を
`compute_call_signature()` に含める。policy 変更でも prompt version が変わり、拒否監査から
どの判定仕様か追跡できるようにする。

本 slice では `safety_settings` を明示変更しない。Gemini が prompt feedback または candidate
finish reason `SAFETY` で遮断した場合は `PROVIDER_SAFETY_FILTER` の block に正規化する。
network、authentication、quota、rate limit、timeout、RECITATION は provider error として扱い、
block audit を作らない。

### 7. worker の question context preparation 直前で短絡する

`run_agent_answer()` の実行順を次にする。

```text
1. acquire run attempt
2. begin live attempt / reset live state
3. read bounded history and extract previous turn
4. build and call InputSafetyService with current question + previous turn
5a. allow:
    - build QuestionContextGenerator
    - prepare QuestionContext
    - build Tavily client / QuestionAnsweringAgent
    - planner / retrieval / answer generation
5b. block:
    - persist fixed refusal assistant message
    - transition running → completed
    - publish completed terminal
    - return
5c. safety check failure:
    - transition running → failed(generation_unavailable)
    - publish failed terminal
    - return
```

block 判定より前に run lifecycle、Redis live attempt、history read が動くのは、既存の非同期
run を安全に所有・終端するために必要である。`それ以降を起動しない` の境界は
QuestionContextGenerator を含む agent 意味処理の構築・呼出しとする。

`InputSafetyService` は QuestionAnsweringAgent の内部 middleware に入れない。middleware にすると
QuestionContextGenerator や agent composition が先に構築され、短絡境界が不明瞭になる。

### 8. block は固定拒否回答を持つ completed run とする

block は infrastructure failure ではなく、safety policy が正常に結論を出した結果である。
そのため `AgentRunStatus.FAILED` や新しい public status を使わない。

固定文:

```text
安全上の理由により、この依頼には対応できません。安全な目的に置き換えた質問であればお手伝いできます。
```

この文は application constant とし、checker に生成させない。reason 別に文面を変えず、内部
policy category を API や UI へ公開しない。

`AgentRunRepository` に run 完了 command として次を追加する。

```python
async def complete_blocked_run(
    self,
    *,
    run_id: UUID,
    answer: str,
    now: datetime | None = None,
) -> bool: ...
```

この command は1 transaction で次を行う。

- thread を lock する。
- 次 seq の assistant message を `content=固定拒否文`、`missing_aspects=[]` で追加する。
- source row は作成しない。
- running run を completed へ遷移し、assistant message を関連づける。
- transition race では既存 `complete_run()` と同じく追加 message を rollback する。

拒否を `AnswerQuestionResult(status="answered")` に偽装しない。通常回答 contract と safety policy
completion は生成理由と不変条件が異なるため、run repository の別 command として表現する。

公開 API / SSE からは従来どおり `completed` と固定 assistant message が見える。新しい
`blocked` status、error code、activity event、answer delta は追加しない。

current user message は API 受付時にすでに thread へ保存済みであり、block 時も削除しない。
本 slice の目的は保存拒否ではなく後続 agent 処理の短絡である。保存前 moderation や拒否入力の
retention policy は別の Problem として扱う。

### 9. 判定処理の失敗は fail closed で後続を止める

次は安全な `allow` が確定していないため、全て後続を起動せず
`AgentRunErrorCode.GENERATION_UNAVAILABLE` で failed にする。

- Gemini configuration / authentication / network / timeout / quota / rate limit error。
- RECITATION 等、safety policy 以外の provider output block。
- 空 response、JSON decode failure、object 以外、unknown enum、extra field。
- `allow + block_reason`、`block + block_reason=None` の組合せ違反。

`generation_unavailable` を再利用するのは、public contract 上は「安全に回答生成を開始できない」
一時的障害であり、frontend に新しい recovery behavior が不要なためである。内部では
`agent_input_safety_failed` log と `result="failed"` metric により通常の生成失敗と区別する。

この経路は content rejection ではないため、block audit を記録しない。task-level retry、元質問の
passthrough、後続 answerer の provider protection への委譲は行わない。

### 10. 監査は block だけ、metric は全最終結果を記録する

監査 contract:

```python
class InputSafetyBlockedAuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["agent_input_safety"] = "agent_input_safety"
    outcome_code: Literal["input_safety_blocked"] = "input_safety_blocked"
    run_id: UUID
    block_reason: InputSafetyBlockReason
    ai_model: str
    prompt_version: str
    input_length: int = Field(ge=1, le=1000)


class InputSafetyAuditRecorder(Protocol):
    async def record_blocked(
        self,
        event: InputSafetyBlockedAuditEvent,
    ) -> None: ...
```

初期 sink は structured structlog event `agent_input_safety_blocked` とし、Logfire の既存 logging
連携へ送る。article pipeline 用 `pipeline_events` へ agent event を混在させない。永続 DB audit と
retention、検索、管理 UI が必要になった場合は、別 slice で agent audit の consumer と schema を
定義する。

監査に含める:

- `run_id`
- `block_reason`
- `ai_model`
- `prompt_version`
- `input_length`

監査に含めない:

- question / previous turn 本文、その head、hash。
- provider raw request / raw response。
- user の秘密情報になり得る自由文 rationale。
- allow event。

metric:

```text
vector.agent.input_safety.outcome
  result = allow | block | failed
  block_reason = none | <InputSafetyBlockReason>
```

全 label は低 cardinality とする。`allow` は監査しないが、拒否率と障害率の分母を得るため
metric は記録する。block audit の保存失敗は
`vector.agent.input_safety.audit_dropped` へ count する。

operational error log は監査とは別であり、checker 障害や audit sink 障害時に記録してよい。
ただし exception message や provider response に質問本文が含まれる可能性を前提に、既存の
error translator が持つ PII-free code / reason だけを field にする。

### 11. composition は safety service と通常 agent を別 builder にする

```python
def build_input_safety_service() -> InputSafetyService: ...
```

- Gemini adapter は builder 内で遅延 import し、API process の import surface を増やさない。
- worker は安全判定前に `build_question_context_generator()` や
  `build_question_answering_agent()` を呼ばない。
- safety checker の model / prompt version は adapter property から audit event へ渡す。
- 既存 `ensure_question_answering_agent_configured()` の認証・認可・provider key 検証を
  簡略化しない。
- safety service が利用する Gemini key 不足は configuration error として fail closed にする。

## API Contract

本 slice は OpenAPI-visible contract を変更しない。

```text
POST /api/v1/research/responses
  Request: ResearchQuestionRequest（変更なし）
  202: ResearchRunStartResponse（変更なし）

GET /api/v1/research/runs/{run_id}
  queued | running | completed | failed（変更なし）

block result:
  run.status = completed
  run.errorCode = null
  assistant message.content = 固定拒否文
  assistant message.sources = []
  assistant message.missingAspects = []
```

`input_safety_result` と `block_reason` は agent 内部 contract であり、API response へ露出しない。
したがって Pydantic API schema と OpenAPI output を変更せず、`/gen-types` は不要である。

## Failure matrix

| condition | run terminal | block audit | downstream agent | user-visible result |
|---|---|---:|---:|---|
| `allow` | current flow | no | start | normal answer |
| `block` | `completed` | yes | do not start | fixed refusal message |
| provider `SAFETY` block | `completed` | yes (`provider_safety_filter`) | do not start | fixed refusal message |
| provider / network / quota failure | `failed(generation_unavailable)` | no | do not start | existing temporary unavailable state |
| response schema invalid | `failed(generation_unavailable)` | no | do not start | existing temporary unavailable state |
| block audit sink failure | `completed` | dropped metric + operational log | do not start | fixed refusal message |
| cancellation wins race | existing cancel terminal | 判定結果に応じた block audit のみ | do not start | existing cancelled state |

## Target implementation surface

### Runtime additions

```text
backend/app/agent/input_safety/__init__.py
backend/app/agent/input_safety/contract.py
backend/app/agent/input_safety/service.py
backend/app/agent/input_safety/audit.py
backend/app/agent/input_safety/metrics.py
backend/app/agent/input_safety/ai/__init__.py
backend/app/agent/input_safety/ai/gemini.py
backend/app/agent/input_safety/ai/prompt.py
backend/app/agent/input_safety/ai/schema_tool.py
backend/app/agent/input_safety/ai/spec.py
```

### Runtime modifications

```text
backend/app/agent/composition.py
  - build_input_safety_service()

backend/app/queue/tasks/agent_run.py
  - bounded historyから直前1往復を抽出
  - current question + previous turnでQuestionContextServiceより前にsafety gateを実行
  - block / failed を return で短絡

backend/app/agent/runs/repository.py
  - complete_blocked_run()
```

`AgentRunStatus`、`AgentRunErrorCode`、API schema、ORM model、migration、Redis stream event union は
変更しない。

## Test specification

### Contract tests

- `allow + block_reason=None` を受理する。
- `block + 各 block_reason` を受理する。
- `allow + block_reason` を拒否する。
- `block + block_reason=None` を拒否する。
- unknown result / reason、extra field を拒否する。

### Prompt / schema tests

- Gemini schema の required / enum / nullable が Pydantic contract と一致する。
- prompt に形式検証、検索計画、回答生成の責務が混入していない。
- question と previous turn の境界タグ、section header が sanitize される。
- assistant `missing_aspects` を prompt に含めない。
- policy prompt が実行能力を高めるかという中心ルールと短い例外だけを持つ。
- 詳細なpolicy境界例をpromptへ埋め込まず、evaluation caseと分離する。
- call signature に prompt、model、generation config、schema が含まれる。

### Adapter tests

- structured `allow` / `block` response を strict contract に変換する。
- finish reason `SAFETY` を `block(provider_safety_filter)` に変換する。
- RECITATION、空 response、not JSON、not object、validation failure を typed failure にする。
- provider exception を既存 Gemini error translator で変換する。
- 1 check につき provider call が1回で、repair retry しない。

### Safety policy evaluation

runtime prompt の unit test とは別に、期待する `input_safety_result` と `block_reason` を持つ
table-driven な evaluation case を管理する。少なくとも次の対を含める。

- 明示的な実行支援block / 同じ有害話題の非実行的分析allow。
- 仮定・role-play・専門家申告を使う実行支援block / 正当な研究・報道allow。
- 防御を名乗る侵入・永続化・検知回避block / 検知・修復・予防allow。
- 危険手順の利用可能性を維持する翻訳block / 分類・高レベル要約allow。
- 自傷手順・奨励block / 安全確保・相談先・予防allow。
- 有害語、罵倒、identity term、prompt injectionへの言及だけを含む入力allow。
- current question単独では曖昧でもprevious turnにより実行支援と分かるfollow-up block。
- previous turnが有害話題でもcurrent questionが防御・支援を求めるfollow-up allow。

high precision を優先するため、block 件数だけでなく false positive を独立して確認する。prompt
調整に使う case と最終確認用の held-out case を分け、全例を prompt の few-shot example へ
転記しない。release 前の固定 regression case では、明確な allow case の過剰拒否と明確な block
case の見逃しをともに0件とする。代表データに対する precision / recall の数値目標は、実測分布を
得る前に任意の値を置かず、evaluation dataset の整備時に別途定める。

### Service tests

- allow は allow metric だけを記録し、audit recorder を呼ばない。
- block は block metric と `record_blocked()` を各1回呼ぶ。
- checker failure は failed metric を記録して例外を伝播する。
- audit recorder failureでも block result を維持する。
- audit event に question / previous turn / raw response が存在しない。

### Repository tests

- `complete_blocked_run()` が assistant message と completed 遷移を同一 transaction で行う。
- refusal message の sources と missing_aspects が空である。
- terminal / cancelled run は idempotent skip する。
- cancel / completion race で追加 assistant message が残らない。
- completed ⇔ assistant message の既存 DB invariant を維持する。

### Worker tests

- historyなしでは `previous_turn=None` をcheckerへ渡す。
- completed previous runでは直前user questionと対応するassistant answerだけを渡す。
- failed previous runでは直前user questionを渡し、さらに前のassistant answerを混ぜない。
- current / previous user / previous assistantを各最大1,000文字に制限する。
- allow のときだけ QuestionContextGenerator と QuestionAnsweringAgent を構築・呼出しする。
- block のとき Context Resolver、Tavily client、Planner、retrieval、answer generator が0 call。
- block は fixed refusal を保存し completed terminal を publish して return する。
- provider `SAFETY` block も同じ短絡経路を通る。
- checker failure は後続0 callで generation_unavailable terminal にする。
- block branch では progress stage、answer delta、question.resolved event を publish しない。
- cancellation / stale attempt の既存 fencing と idempotency を壊さない。
- `import app.main` が google SDK を eager import しない既存契約を維持する。

### API / frontend regression

- OpenAPI の research run status / errorCode union が変わらない。
- block 完了後の thread detail が固定 assistant message を通常 message として返す。
- frontend generated types に差分がない。

## Work plan

1. `input_safety` contract、block reason、validator、port を追加する。
2. Gemini prompt / schema / spec / adapter を structured output 1 call で実装する。
3. service、block-only audit recorder、outcome metric を追加する。
4. `complete_blocked_run()` と transaction / race tests を追加する。
5. composition に遅延 builder を追加する。
6. worker の history read 後に直前1往復を抽出してsafety gateへ渡し、block / failed の早期
   return を追加する。
7. contract、prompt、adapter、service、worker、repository test を追加する。
8. `/check` で format、lint、types、tests を検証する。

## Done

- 全 research run が QuestionContextService より前に `InputSafetyService` を通る。
- `input_safety_result` は `allow | block` 以外を受け付けない。
- `block` は固定拒否 message を持つ completed run として終端する。
- block branch で Context Resolver、Planner、検索、通常回答生成が0 callであることをtestが証明する。
- safety 判定が失敗した場合も後続へ進まない。
- block だけが拒否監査を生成し、allow は audit recorder を呼ばない。
- metric から allow / block / failed の件数と拒否率を算出できる。
- audit / log / metric に question、previous turn、raw response が含まれない。
- API、DB schema、Redis schema、frontend type に差分がない。
- 新規 dependency、環境変数、認証・認可変更がない。
- backend `/check` がgreenである。

## 公式参考資料

- Gemini API safety settings:
  https://ai.google.dev/gemini-api/docs/safety-settings
- Gemini API structured outputs:
  https://ai.google.dev/gemini-api/docs/structured-output
- Google Gen AI Python SDK:
  https://googleapis.github.io/python-genai/
- Google Responsible Generative AI Toolkit — Design a responsible approach:
  https://ai.google.dev/responsible/docs/design
- Google Responsible Generative AI Toolkit — Safeguard your models:
  https://ai.google.dev/responsible/docs/safeguards
