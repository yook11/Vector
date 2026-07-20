# Agent input safety gate slice 仕様

更新日: 2026-07-20

実装状況: Implemented — 2026-07-20

## 位置付け

Vector の research agent は、user message を保存して非同期 run を作成した後、worker が
persistent run lifecycle を所有し、意味的な回答 workflow を `AnsweringRunner.run()` へ委譲する。

本 slice は `AnsweringRunner` の明示的な先頭 phase に `input_safety` 境界を追加し、現在の入力を
`allow` / `block` の2値で判定する。`block` の場合は `InputSafetyBlocked` を正常な停止制御として
raiseし、workerがrunを `policy_blocked` で終端する。assistant messageは保存せず、frontendは永続化された
run statusからpolicy noticeを表示する。question context preparation、planner、検索、回答生成は
一切起動しない。

2026-07-14 のユーザー合意:

- 空文字・文字数・型などの決定的検証は既存の Pydantic schema が担い、LLM に判定させない。
- LLM の責務は入力内容を `allow` / `block` で判定することだけに限定する。
- 判定 field 名は `input_safety_result` とする。
- `block` 後はQuestion Context Agent、Planner、検索、通常回答生成を一切起動しない。
- 拒否記録はblock時のstructured logだけとし、`allow` は正常系として記録しない。

2026-07-20 のユーザー合意:

- Input Safety Agent を `AnsweringRunner.run()` の最初の意味的・LLM phase とする。
- checker は `InputSafetyCheckResult` を返し、Runner が `safety_check.is_blocked` を確認する。
- block は `RunResult` に混ぜず、Runner が `InputSafetyBlocked` をraiseする。
- `block_reason` は判定条件ではなく、block確定後に停止制御と監査へ渡す付随情報とする。
- workerは `InputSafetyBlocked` をcatchし、assistant messageを作らず `policy_blocked` terminalへ遷移する。
- `policy_blocked` はsafety policyによる確定的な停止を表し、infrastructure failureの `failed`、
  回答生成済みの `completed` と区別する。
- frontendは `policy_blocked` statusから固定noticeを表示し、会話messageとして永続化しない。

前提仕様:

- `backend/specs/agent-question-context-preparation-slice.md`
- `backend/specs/agent-history-run-execution-slice.md`
- `backend/specs/agent-threads-runs-boundary-slice.md`
- `backend/specs/agent-live-stream-transport-slice.md`
- `backend/specs/agent-answering-runner-boundary-slice.md`
- `backend/specs/agent-declaration-runner-orchestration-slice.md`
- `backend/specs/agent-audit-recorder-removal-slice.md`

本仕様は、`agent-answering-runner-boundary-slice.md` §12 が将来形として記載した「safety判定後に
Runnerを構築する」という配置を上書きする。現在は `AnsweringRunner` が全semantic workflowの
唯一のownerであり、input safetyもその先頭phaseとして明示する。一方、allow前に通常Agentの
runtimeや外部resourceを起動しない短絡条件は維持する。

## Problem

現在の research agent には次の防壁がある。

- API schema が質問を strip し、空文字と1,000文字超過を 422 で拒否する。
- LLM prompt に埋め込む入力は `<untrusted_input>` と
  `sanitize_for_untrusted_block()` で命令境界から分離する。
- answerer は内部実装、system instruction、API key を開示しないよう指示される。

これらは形式検証または prompt injection の影響低減であり、ユーザーが求める支援内容を
処理してよいかは判定していない。そのため、明確に有害な実行支援でもQuestion Context Agent、
Planner、検索、回答生成が起動し、不要な外部 call と不適切な回答生成の余地が生じる。

また、provider 自身の safety filter は provider 固有の確率判定であり、Vector が定義する
許可・拒否境界の SSoT にはできない。Vector の用途に合わせた application policy と、
provider が API call 自体を遮断した場合の扱いを分けて定義する必要がある。

policy blockはassistantが回答した事実ではないため、固定拒否文をconversation messageとして保存すると、
run outcomeと会話内容が混同される。blockした事実を再接続後も再現しつつ、表示文言はfrontendのnoticeとして
導出できる永続状態が必要である。

## Evidence

- `app/schemas/research.py::ResearchQuestion` は strip、`min_length=1`、
  `max_length=1000` を保証する。
- `app/agent/router.py::create_research_response()` は user message と queued run を同一
  transaction で保存した後に worker task を enqueue する。
- `app/queue/tasks/agent_run.py::run_agent_answer()` は run attempt と履歴を取得し、
  `build_answering_runner()` でRunnerを構築して `AnsweringRunner.run()` を1回呼ぶ。
- `app/agent/running/answering_runner.py::AnsweringRunner.run()` は現在、
  `QuestionContextService.prepare()` を最初の意味的・LLM工程として実行する。
- `app/agent/running/contract.py::RunResult` は最終 `AnswerQuestionResult` と、準備済みの
  `AnsweringRunContext` を必須で持つ。context準備前のblockをこの型へ混ぜると意味が崩れる。
- `backend/tests/agent/test_agent_run_task.py` はworkerがsemantic executionを
  `build_answering_runner()` / `AnsweringRunner.run()` だけへ委譲する境界を固定している。
- `app/agent/question_context/service.py` は context generator 失敗時に元質問を passthrough
  する。この fallback は会話文脈準備には適切だが、安全判定失敗時には適用できない。
- `app/analysis/prompt_safety.py` は prompt 境界の無害化を担い、内容の許可・拒否は行わない。
- `app/agent/runs/repository.py::complete_run()` は assistant message 作成と run completed 遷移を
  同一 transaction で行い、completed run が必ず assistant message を持つ DB invariant を守る。
- `app/models/agent_run.py` の公開状態機械は `queued | running | completed | failed` であり、
  completed と assistant message の存在が DB CHECK で結ばれている。
- `app/agent/live_updates/stream.py` は completed / failed の terminal event を公開する。
- `app/schemas/research.py::ResearchRunStatus` と `ResearchMessageRun.status` は同じstatus unionをAPIへ
  公開し、frontend generated typeとlive controllerがその値をexhaustiveに扱う。
- thread detailはuser messageにrun projectionを持つため、assistant messageがなくても
  `policy_blocked`を該当turnへ表示できる。
- `app/agent/agent.py` と `app/agent/runtime/` は、役割別の不変な `Agent` 宣言、手動prompt version、
  provider-neutralな `AgentRuntime`、structured output validationの共通境界を持つ。
- `GeminiAgentRuntime.invoke()` はcandidate finish reasonの `SAFETY` / `RECITATION` を分類する一方、
  non-streaming responseの `prompt_feedback.block_reason` はまだ判定していない。
- agentの差し替え可能なaudit recorder層は2026-07-18に撤去済みで、attempt個票はspan、集計はmetric、
  明示的に必要なblock監査はstructured logで記録する方針である。
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
2. `block` はassistant messageを作らず `policy_blocked` で終端し、後続LLM、検索、toolを起動しない。
3. 拒否理由は低 cardinality の安定した code としてstructured logで追跡できる。
4. `allow` は成功記録を残さず、内容を持たない集計 metric のみ記録する。
5. checker の正常な contract 出力を得られない場合や provider 障害を `allow` とみなさず、
   安全境界を fallback で迂回しない。
6. 通常回答の `RunResult` contractを変更せず、blockを型付きの正常停止制御として表現する。
7. policy blockをDB、polling API、SSEの共通terminal statusとして再現可能にし、frontendが
   conversation messageではないnoticeとして表示できるようにする。

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
build AnsweringRunner
             │
             ▼
AnsweringRunner.run
  ├─ InputSafetyChecker.check             # first semantic phase
  │    ├─ allow  ───────────────────────────────┐
  │    └─ block → raise InputSafetyBlocked      │
  │                                             ▼
  │                                  QuestionContextService
  │                                             ▼
  │                                  Planner → retrieval → answer
  │                                             ▼
  │                                          RunResult
  └─ InputSafetyBlocked
       └─ worker catch → policy_blocked persistence → policy_blocked terminal
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

- Input Safety Agentを `AnsweringRunner.run()` の最初の意味的・LLM phaseとする。
- `input_safety_result == "allow"` が確定するまで `QuestionContextService.prepare()`、Planner、
  retrieval、answer Agentを呼び出さない。
- allow確定前にQuestion Context、Planner、Direct / Evidence Answer、External Query / Selector用の
  provider runtime、Tavily client、internal embeddingを生成・activateしない。
- immutableなAgent宣言、provider resourceを開かないService、`AnsweringRunner` objectの構築は
  起動とは扱わない。
- block branchで許可する副作用は、run lifecycle、拒否structured log、terminal通知、metricに限定する。
- blank / length / type 判定を input safety prompt に重複させない。
- checker の会話入力は current question と optional な previous turn だけに限定し、それ以前の
  履歴を渡さない。
- previous turn は current question の参照解決にだけ使い、previous turn の内容だけを理由に
  current question を block しない。
- checker は tool、検索、URL context、code execution を利用しない。
- checker の出力を application の strict contract で検証する。
- checker 失敗、timeout、rate limit、schema 不正を `allow` へ fallback しない。
- checkerは判定結果を返し、Runnerが `safety_check.is_blocked` を主判定として制御する。
- `block_reason` の有無からblockを推測せず、block確定後の付随情報としてだけ参照する。
- blockは `RunResult`、`AnswerQuestionResult`、optionalな `AnsweringRunContext` で表現しない。
- blockではassistant message、message source、永続化用message draftを作らない。
- `InputSafetyBlocked` は正常な停止制御であり、`agent_answering_run` spanをERRORにせず、
  exception eventを記録せず、同じexception instanceをworkerへ伝播する。
- workerは `InputSafetyBlocked` をgeneric exceptionより前にcatchし、Taskiqへ漏らさない。
- `allow` の成功記録を作成しない。
- 拒否structured log、通常log、metric attributeに質問本文、履歴本文、provider raw responseを含めない。
- blockはuserの入力エラーやinfrastructure failureではなく、`policy_blocked`として表す確定terminalである。
- `policy_blocked` runは `assistant_message_id=None`、`error_code=None` とし、`completed`や`failed`にしない。
- DB、polling API、Redis terminal event、frontend generated typeで同じ `policy_blocked` 語彙を使う。
- `block_reason` はDB、API、Redis、frontendへ公開せず、block-only structured logにだけ記録する。
- 既存の prompt untrusted boundary と provider safety protection は防御層として維持する。

## Non-goals

- DB column、table、indexの追加。既存status CHECKへ `policy_blocked` を追加するmigrationだけを行う。
- APIにblock reasonや専用error responseを追加すること。
- frontendにreason表示、異議申立てUI、dismiss stateを追加すること。
- user message を保存前に同期 moderation し、HTTP request 自体を拒否すること。
- user profile、cross-thread 履歴、外部データを使ったリスク評価。
- 直前1往復より前の thread 履歴を使ったリスク評価。
- allowされた質問の成功監査を保存すること。
- raw input または raw model response を監査目的で複製保存すること。
- prompt injection、jailbreak、機密情報漏えい対策をこの判定だけに依存すること。
- policy reason ごとに異なる拒否文を生成すること。
- human review queue、appeal、管理画面を作ること。
- 既存 answerer の provider safety settings を変更すること。
- generic guardrail middleware、hook registry、別のtop-level Runnerを追加すること。
- `RunResult`をanswered / blockedのunionへ変更すること。
- agent audit recorder Protocolや永続audit tableを再導入すること。

## 設計判断

### 1. package 名は `input_safety` とする

```text
backend/app/agent/input_safety/
├── __init__.py
├── agent.py
├── contract.py
├── service.py
├── metrics.py
├── prompts.py
└── ai/
    ├── __init__.py
    └── schema_tool.py
```

`moderation` は provider API や運用上の審査全般にも読める。今回の package は agent input の
安全判定に限定され、ユーザー合意済み field 名も `input_safety_result` であるため、
境界名を `input_safety` に揃える。

### 2. strict contract は2値判定と拒否理由だけを持つ

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

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


INPUT_SAFETY_POLICY_BLOCK_REASONS = (
    InputSafetyBlockReason.DANGEROUS_OR_ILLEGAL_INSTRUCTIONS,
    InputSafetyBlockReason.CREDENTIAL_OR_PRIVACY_ABUSE,
    InputSafetyBlockReason.TARGETED_HATE_OR_HARASSMENT,
    InputSafetyBlockReason.SEXUAL_EXPLOITATION,
    InputSafetyBlockReason.SELF_HARM_INSTRUCTIONS,
)


class InputSafetyAgentOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_safety_result: InputSafetyResult
    block_reason: InputSafetyBlockReason | None = None

    @model_validator(mode="after")
    def validate_block_reason(self) -> Self:
        if self.input_safety_result is InputSafetyResult.BLOCK:
            if self.block_reason is None:
                raise ValueError("block result must include block reason")
            if self.block_reason not in INPUT_SAFETY_POLICY_BLOCK_REASONS:
                raise ValueError("agent output must include policy block reason")
        elif self.block_reason is not None:
            raise ValueError("allow result cannot include block reason")
        return self


class InputSafetyCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_safety_result: InputSafetyResult
    block_reason: InputSafetyBlockReason | None = None

    @property
    def is_blocked(self) -> bool:
        return self.input_safety_result is InputSafetyResult.BLOCK

    @model_validator(mode="after")
    def validate_block_reason(self) -> Self:
        if self.is_blocked:
            if self.block_reason is None:
                raise ValueError("block result must include block reason")
        elif self.block_reason is not None:
            raise ValueError("allow result cannot include block reason")
        return self


class InputSafetyPreviousTurn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_question: str = Field(min_length=1, max_length=1000)
    assistant_answer: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
    )


@dataclass(frozen=True, slots=True)
class InputSafetyAgentInput:
    question: str
    previous_turn: InputSafetyPreviousTurn | None


class InputSafetyChecker(Protocol):
    async def check(
        self,
        *,
        question: str,
        previous_turn: InputSafetyPreviousTurn | None,
        run_id: UUID,
    ) -> InputSafetyCheckResult: ...


class InputSafetyBlocked(Exception):
    def __init__(self, *, block_reason: InputSafetyBlockReason) -> None:
        self.block_reason = block_reason
        super().__init__("input safety blocked")
```

`decision` / `result` 単独では何の結果かが読めないため、field は
`input_safety_result` とする。class 名が safety 文脈を与えても、schema、audit、test failure
から field 単独で現れる場合に意味が維持される名前を優先する。

`block_reason` は自由文にしない。自由文は表現揺れ、high-cardinality metric、質問本文の
引用、監査上の機密情報混入を起こし得る。reason code 自体を軽い説明として扱い、ユーザーへ
見せる文言には使わない。

`InputSafetyAgentOutput` はmodelが返せるwire contract、`InputSafetyCheckResult` はcheckerが
Runnerへ返すapplication contractである。ただし `block_reason` は「なぜblockしたか」を表す同一の
ドメイン概念なので、両contractで `InputSafetyBlockReason` を共有する。modelが返せる値は
`INPUT_SAFETY_POLICY_BLOCK_REASONS` の5理由だけに制限し、provider自身の遮断を表す
`PROVIDER_SAFETY_FILTER` はresponse schemaとAgent output validationの両方で拒否する。
serviceは検証済みAgent出力のreasonを変換せずに渡し、provider errorから正規化した場合だけ
`PROVIDER_SAFETY_FILTER` を生成できる。

`input_safety_result` はmodel wire contract上の明示的な主判定である。application側は
`is_blocked` でその判定を読み、`block_reason` の有無を判定条件にしない。`is_blocked` はPydantic
fieldではないため、serialized outputやGemini response schemaには含めない。

`InputSafetyChecker` は `AnsweringRunner` が最初に呼ぶrole phase portである。
`InputSafetyService` が不変な `INPUT_SAFETY_AGENT` と共通 `AgentRuntime` を使ってこのportを実装し、
testはfake checkerへ差し替える。provider固有の `GeminiInputSafetyChecker` は追加しない。

`InputSafetyBlocked` はpolicy blockをRunnerからworkerへ運ぶ正常な停止制御である。名前に`Error`を
付けず、自由文、question、previous turn、provider response、`RunResult`を保持しない。

`PROVIDER_SAFETY_FILTER` は Vector の safety checker がstructured Agent出力を得る前に、providerが
promptまたはcandidateをsafety reasonで遮断した場合だけserviceが生成できる保守的なblock理由である。
modelのresponse schemaやinstructionsへこの値を公開しない。RECITATION、schema不正、空responseは
safety blockと混同せず、判定失敗として扱う。

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
- previous runが `policy_blocked` の場合も直前user questionだけを採用し、さらに前のassistant answerを
  誤って組み合わせない。
- previous turn は current question の参照先を理解するためだけに使い、過去 turn を再判定しない。
- 過去に危険な話題があるだけで current question を block しない。
- assistant `missing_aspects` は安全判定に不要なため渡さない。
- question / previous turn は `sanitize_for_untrusted_block()` を通した後、各fieldを1つのJSON stringとして
  encodeし、改行やfield labelをdata内部へ閉じ込めたうえで全てuntrusted dataとして囲う。

worker は既存 bounded history を1回だけDBから読み、`RunInput.history`としてRunnerへ渡す。
`AnsweringRunner` がsafety checkの直前に `_previous_turn(input.history)` で末尾からprevious turnを
投影する。追加queryは行わない。直前のuser messageの直後にassistant messageがある場合だけ同じ
turnのanswerとして採用する。workerはprevious turnの意味的な投影を所有しない。

### 5. `InputSafetyService` は1回判定し、fallback / repair retry しない

```python
class InputSafetyService:
    def __init__(
        self,
        *,
        agent: Agent[InputSafetyAgentInput, InputSafetyAgentOutput],
        runtime_scope_factory: AgentRuntimeScopeFactory,
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

1. `AgentRuntime` scopeを1回だけactivateする。
2. `INPUT_SAFETY_AGENT` を `attempt_number=1` で1回だけinvokeする。
3. strict `InputSafetyAgentOutput` をapplicationの `InputSafetyCheckResult` へ正規化して返す。
4. `allow` / `block` / `failed` のmetricを最終結果ごとに1回記録する。
5. `safety_check.is_blocked` の場合だけ `agent_input_safety_blocked` structured logを記録する。

`InputSafetyService` は `InputSafetyChecker` portの具象であり、Agentが返した主判定を書き換えない。
block時にも `InputSafetyBlocked` をraiseせず、検証済みcheck resultをRunnerへ返す。意味的な判定は
Input Safety Agent、workflowの短絡は `AnsweringRunner` が所有する。

check result は2 field の小さい structured output であり、repair retry はコストと安全境界の複雑さに
見合わない。response不正は `AgentResponseInvalidError`、provider障害は既存 `AIProviderError` として
Runnerを貫通してworkerへ伝播し、後続を起動せず
`generation_unavailable` で run を failed にする。

### 6. `INPUT_SAFETY_AGENT` と共通 `AgentRuntime` を使う

初期実装は既存の question context / planner と同じ `gemini-2.5-flash-lite` を使い、新規 model、
provider、dependency、API key を追加しない。

役割は既存Agent宣言と同じ形で固定する。

```python
INPUT_SAFETY_PROMPT = AgentPrompt[InputSafetyAgentInput](
    version=INPUT_SAFETY_PROMPT_VERSION,
    instructions=INPUT_SAFETY_INSTRUCTIONS,
    input_renderer=render_input_safety_input,
)

INPUT_SAFETY_AGENT: Agent[
    InputSafetyAgentInput,
    InputSafetyAgentOutput,
] = Agent(
    name="input_safety",
    prompt=INPUT_SAFETY_PROMPT,
    model=ModelTarget(provider="gemini", name="gemini-2.5-flash-lite"),
    model_settings=ModelSettings(temperature=0.0, max_output_tokens=128),
    output_type=InputSafetyAgentOutput,
    response_schema=INPUT_SAFETY_GEMINI_SCHEMA,
)
```

```text
model: gemini-2.5-flash-lite
temperature: 0.0
max_output_tokens: 128
response_mime_type: application/json
response_schema: INPUT_SAFETY_GEMINI_SCHEMA
tools: none
retry: none
```

provider call、JSON parse、Pydantic validation、attempt span、usage記録、provider error translationは
共通 `GeminiAgentRuntime.invoke()` を再利用する。input safety専用Gemini client、adapter、retry、
call signature機構を追加しない。

schema の required field は `input_safety_result` と `block_reason` の2つとする。
`block_reason` はapplication policy由来の5理由だけを持つnullable enumとし、Agent outputの
model validatorがresultとの組合せを最終検証する。`provider_safety_filter` はschemaへ含めない。
promptにJSON schema本文や出力例を重複記載せず、policyと判断規則だけを記載する。

runtime promptには「要求された出力が有害行為の実行能力を実質的に高めるか」という中心ルール、
短いblock reason定義、それ自体ではblockしない例外だけを記載する。policy境界の詳細例はpromptへ
大量に埋め込まず、test / evaluation caseで管理する。checkerへ渡すuser dataはcurrent question
最大1,000文字、previous user question最大1,000文字、previous assistant answer最大1,000文字の
合計最大3,000文字とする。

prompt versionは既存Agent宣言と同じ手動revisionとし、固定instructions、固定input template、
response schemaのmodel-visibleな構造を変えるときに更新する。modelとprompt versionは共通runtimeの
attempt spanおよびblock structured logから追跡する。

本 slice では `safety_settings` を明示変更しない。Geminiがprompt feedbackまたはcandidate
finish reason `SAFETY` で遮断した場合は、Agent outputを経由せず
`PROVIDER_SAFETY_FILTER` のblockに正規化する。
network、authentication、quota、rate limit、timeout、RECITATION は provider error として扱い、
block structured logを作らない。

この契約を共通runtimeで満たすため、non-streaming `GeminiAgentRuntime.invoke()` はusage記録後、
candidate finish reasonより前に `prompt_feedback.block_reason` を確認し、
`AIProviderInputRejectedError(reason=INPUT_BLOCKED)` へ分類する。Gemini translator / runtimeは
`INPUT_BLOCKED` とfinish reason `SAFETY` にだけprovider-neutralな
`AIProviderContentRejectionKind.SAFETY`を付ける。InputSafetyServiceはGemini固有enumを参照せず、
input / output content errorのこの共通分類だけを `PROVIDER_SAFETY_FILTER` に正規化する。
その他のinput rejectionやRECITATIONは `OTHER` のままとし、正規化しない。

### 7. `AnsweringRunner` の先頭phaseで判定し、blockをraiseする

Runnerの主制御は次の形とする。

```python
safety_check = await self._input_safety_checker.check(
    question=input.question,
    previous_turn=_previous_turn(input.history),
    run_id=run_context.run_id,
)

if safety_check.is_blocked:
    assert safety_check.block_reason is not None
    raise InputSafetyBlocked(
        block_reason=safety_check.block_reason,
    )

preparation = await self._context_preparer.prepare(...)
```

`is_blocked` が制御上の主判定であり、`block_reason` はblock確定後に例外へ渡す付随情報である。
`assert` はstrict validatorが保証した型のnarrowingだけを目的とし、blockの判定には使わない。

全体の実行順を次にする。

```text
1. acquire run attempt
2. begin live attempt / reset live state
3. read bounded history
4. build AnsweringRunner without activating provider/search resources
5. AnsweringRunner.run
   5.1 extract previous turn
   5.2 call InputSafetyChecker
   5.3a allow:
        - prepare QuestionContext
        - build answering phases
        - planner / retrieval / answer generation
        - return RunResult
   5.3b block:
        - raise InputSafetyBlocked
   5.3c safety check failure:
        - propagate typed provider / response error
6a. worker catches InputSafetyBlocked:
    - transition running → policy_blocked
    - publish policy_blocked terminal
    - return
6b. worker receives safety check failure:
    - transition running → failed(generation_unavailable)
    - publish failed terminal
    - return
```

block 判定より前に run lifecycle、Redis live attempt、history read が動くのは、既存の非同期
run を安全に所有・終端するために必要である。RunnerとimmutableなAgent宣言の構築はprovider callや
Tool起動ではない。`それ以降を起動しない` の境界はQuestion Context Agentを含む、input safety以外の
意味的phase、provider runtime、Tool resourceのinvoke / activationとする。

input safetyをmiddlewareやhookへ隠さず、`AnsweringRunner.run()` から最初の工程として読める形にする。
Runnerが受け取る `InputSafetyChecker` はprovider-neutralなportであり、RunnerはGemini具象、DB、Redis、
Taskiqをimportしない。

`InputSafetyBlocked` は `RunResult` の代替値ではない。`_answering_run_span()` は
`AnswerGenerationStopped` と同様にこの例外をspan内で捕捉し、ERROR statusやexception eventを付けず、
span終了後に同じinstanceをworkerへ再送出する。

### 8. block はmessageではなく `policy_blocked` terminal statusとして永続化する

blockはinfrastructure failureでも回答完了でもなく、safety policyが正常に拒否を確定した結果である。
run状態機械へ次のterminal statusを追加する。

```python
class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    POLICY_BLOCKED = "policy_blocked"
    FAILED = "failed"
```

`blocked`単独では一時停止、quota、human approval待ち、外部dependency待ちにも読める。現在の原因を
明示するため `policy_blocked` とする。`input_safety_blocked` までは狭めず、applicationまたはproviderの
safety policyで確定拒否されたrunに共通する永続語彙とする。

workerは `InputSafetyBlocked` をcatchし、run repositoryの状態遷移commandを呼ぶ。

```python
async def mark_policy_blocked(
    self,
    run_id: UUID,
    *,
    expected_attempt_epoch: int,
    now: datetime | None = None,
) -> bool: ...
```

このcommandは1 transaction内で、`status=running`かつ
`attempt_epoch=expected_attempt_epoch`のrunだけを次へ更新する。

```text
status = policy_blocked
assistant_message_id = null
error_code = null
progress_stage = null
completed_at = now
```

該当rowがない場合は `False` を返す。別attempt、cancel、既存terminalを上書きしない。
message、source、thread rowは作成・更新しないため、block branchでthread lockとmessage sequence採番は
行わない。

DB migrationは `ck_agent_runs_status` を次へ更新する。

```sql
status IN ('queued', 'running', 'completed', 'policy_blocked', 'failed')
```

upgrade / downgradeはlock待ちとconstraint scanの両方に有限timeoutを設定する。offline downgrade SQLは
`policy_blocked` rowの存在をPostgreSQL `DO` blockで検査し、存在時は暗黙変換せずfail closedに停止する。

既存の双方向制約は変更しない。

```text
status = completed ⇔ assistant_message_id is not null
status = failed    ⇔ error_code is not null
```

したがって `policy_blocked` ではassistant messageとerror codeがともに存在しないことをDBが保証する。
`completed_at` は既存どおり全terminalの確定時刻として使い、新しいtimestamp columnは追加しない。

拒否を `AnswerQuestionResult`、`RunResult`、`AnsweringRunContext`、assistant messageへ変換しない。
`block_reason`もrun rowへ保存しない。frontendに必要なのはpolicyで拒否されたという安定したstatusだけで、
詳細reasonはblock structured logの責務である。

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

この経路は content rejection ではないため、block structured logを記録しない。task-level retry、元質問の
passthrough、後続 answerer の provider protection への委譲は行わない。

workerは `AgentResponseInvalidError` を既存provider errorと同じ
`generation_unavailable` 分類へ追加する。`InputSafetyBlocked` はこの分類より前にcatchし、failedへ
落とさない。InputSafetyServiceはfailure metricを記録して元のtyped exceptionをそのまま伝播する。

### 10. 監査は block だけ、metric は全最終結果を記録する

blockが確定した場合だけ、InputSafetyServiceがstructured structlog event
`agent_input_safety_blocked` を1件記録し、Logfireの既存logging連携へ送る。差し替え可能なsinkや永続
consumerが存在しないため、`InputSafetyAuditRecorder` Protocol、event model、best-effort recorder
配管は追加しない。article pipeline用 `pipeline_events` へagent eventを混在させない。

永続DB audit、retention、検索、管理UIが必要になった場合は、それを読むconsumerを定義する別sliceで
schemaと保存境界を設計する。

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
metric は記録する。共通AgentRuntimeのattempt spanはprovider callの技術的telemetryであり、
input safetyの成功監査eventとは扱わない。

operational error log は監査とは別であり、checker障害時に記録してよい。
ただし exception message や provider response に質問本文が含まれる可能性を前提に、既存の
error translator が持つ PII-free code / reason だけを field にする。

### 11. composition は safety checker を `AnsweringRunner` へ配線する

```python
return AnsweringRunner(
    input_safety_checker=InputSafetyService(
        agent=INPUT_SAFETY_AGENT,
        runtime_scope_factory=activate_gemini_agent_runtime,
    ),
    context_preparer=QuestionContextService(...),
    phases_factory=...,
)
```

- workerが呼ぶsemantic factoryは引き続き `build_answering_runner()` だけとし、別の
  `build_input_safety_service()` を公開しない。
- `INPUT_SAFETY_AGENT` はimmutable / statelessな宣言であり、composition時にimportしてよい。
- `activate_gemini_agent_runtime` はService構築時に呼ばず、safety check時に初めてscopeを開く。
- `_build_answering_phases()` はallow後まで呼ばない既存lazy boundaryを維持する。
- Question Context用runtimeが未設定時にsafe fallbackする既存契約を、input safetyへ流用しない。
  safety用Gemini key不足はconfiguration errorとしてfail closedにする。
- Agent、runtime、Serviceのmodel / prompt versionは `INPUT_SAFETY_AGENT` をSSoTとし、adapter属性探索や
  call signatureを追加しない。

## API Contract

`ResearchRunStatus`へ `policy_blocked` を追加する。field追加ではなくenum member追加だが、exhaustiveな
consumerには互換性影響があるため、backend、OpenAPI generated type、frontendを同一sliceで更新する。

```text
POST /api/v1/research/responses
  Request: ResearchQuestionRequest（変更なし）
  202: ResearchRunStartResponse（変更なし）

GET /api/v1/research/runs/{run_id}
  queued | running | completed | policy_blocked | failed

block result:
  run.status = policy_blocked
  run.errorCode = null
  run.progressStage = null
  run.recentEvents = []

GET /api/v1/research/threads/{thread_id}
  user message.run.status = policy_blocked
  corresponding assistant message = absent

SSE terminal event:
  { attemptEpoch, status: "policy_blocked" }
  errorCode field = absent
```

terminal transport contractは `completed | policy_blocked` で `errorCode` を禁止し、`failed` では必須とする。
pollingは `policy_blocked` の場合にbest-effort Redis recent eventを読まず、旧attemptのsemantic eventを
public responseへ混入させない。

`input_safety_result` と `block_reason` は agent 内部 contract であり、API response へ露出しない。
`policy_blocked` はrun lifecycleのpublic statusであり、policy reasonではない。Pydantic schemaをSSoTとして
OpenAPIを更新し、`/gen-types`でfrontend typeを同期する。

### Frontend presentation

frontendは `policy_blocked` をassistant answerやerrorとして描画せず、該当user turnのanswer slotに
status noticeとして次の固定文を表示する。

```text
この依頼は安全上のポリシーにより処理されませんでした。
```

- noticeはfrontend constantとし、backendから本文を返さない。
- `block_reason`別に文言を変えない。
- live draftが存在する場合は即座に破棄し、`draftMode=suppressed`とする。
- completed用のfinalizing表示、assistant bubble、source panel、missing aspects、回答完了announcementを出さない。
- SSE、polling、thread初回表示、再接続、reloadの全経路で同じ `policy_blocked` statusから再現する。
- noticeはconversation messageではなくrun statusのpresentationであり、copy対象のassistant answerや
  会話履歴本文として扱わない。
- accessibility treeでは固定文をstatus noticeとして1回通知し、同じrunのSSEとpolling重複で
  再announcementしない。

## Failure matrix

| condition | run terminal | block structured log | downstream phase | user-visible result |
|---|---|---:|---:|---|
| `allow` | current flow | no | start | normal answer |
| `block` | `policy_blocked` | yes | do not start | policy notice |
| provider `SAFETY` block | `policy_blocked` | yes (`provider_safety_filter`) | do not start | policy notice |
| provider / network / quota failure | `failed(generation_unavailable)` | no | do not start | existing temporary unavailable state |
| response schema invalid | `failed(generation_unavailable)` | no | do not start | existing temporary unavailable state |
| cancellation wins persistence race | existing cancel terminal | block判定済みならyes | do not start | existing cancelled state |

## Target implementation surface

### Runtime additions

```text
backend/app/agent/input_safety/__init__.py
backend/app/agent/input_safety/agent.py
backend/app/agent/input_safety/contract.py
backend/app/agent/input_safety/service.py
backend/app/agent/input_safety/metrics.py
backend/app/agent/input_safety/prompts.py
backend/app/agent/input_safety/ai/__init__.py
backend/app/agent/input_safety/ai/schema_tool.py
```

### Runtime modifications

```text
backend/app/agent/composition.py
  - InputSafetyServiceをbuild_answering_runner()へ配線

backend/scripts/probe_question_answering.py
  - direct / external probeのRunnerにも同じInputSafetyServiceを配線

backend/app/agent/running/answering_runner.py
  - input safetyを最初のphaseとして実行
  - blockでInputSafetyBlockedをraise
  - allow後だけQuestionContextService以降を実行

backend/app/agent/runtime/gemini.py
  - non-streaming invokeのprompt_feedback block分類を追加

backend/app/queue/tasks/agent_run.py
  - InputSafetyBlockedを正常停止としてcatch
  - mark_policy_blocked()でrunだけをterminalへ遷移
  - commit後にpolicy_blocked terminalをpublish
  - AgentResponseInvalidErrorをgeneration_unavailableへ分類

backend/app/agent/runs/repository.py
  - attempt epochでfenceしたmark_policy_blocked()
  - policy_blockedをterminal / cancellation分類へ追加

backend/app/agent/runs/types.py
backend/app/agent/runs/projection.py
backend/app/agent/runs/contracts.py
backend/app/schemas/research.py
backend/app/models/agent_run.py
backend/app/agent/live_updates/stream.py
  - policy_blocked statusをDB / API / SSEの共通語彙へ追加
  - CancelRunOutcome.ALREADY_POLICY_BLOCKEDを追加し、cancel APIは204でidempotentに扱う

backend/app/agent/live_updates/sse.py
backend/app/agent/router.py
  - queued接続中のDB再確認とSSE接続前preflightでpolicy_blockedをterminalとして収束

backend/alembic/versions/y5_agent_run_policy_blocked.py
  - ck_agent_runs_statusへpolicy_blockedを追加

frontend/src/features/research/live/events.ts
frontend/src/features/research/live/reducer.ts
frontend/src/features/research/live/controller.ts
frontend/src/features/research/components/ResearchThreadLiveBoundary.tsx
frontend/src/features/research/components/ResearchLiveAnnouncer.tsx
frontend/src/types/types.gen.ts
  - policy_blocked terminalを収束させ、assistant messageではないpolicy noticeを表示
```

新しいcolumn、error code、block reason field、assistant message種別は追加しない。

## Test specification

### Contract tests

- `allow + block_reason=None` を受理する。
- Agent outputは `block + application policy由来の各block_reason` を受理する。
- Agent outputは `provider_safety_filter` をunknown reasonとして拒否する。
- application check resultはservice内部で生成した `block(provider_safety_filter)` を受理する。
- `allow + block_reason` を拒否する。
- `block + block_reason=None` を拒否する。
- unknown result / reason、extra field を拒否する。
- allowでは `is_blocked is False`、blockでは `is_blocked is True` となる。
- `is_blocked` がserializationとGemini schemaに含まれない。
- `InputSafetyBlocked` がblock reasonだけを保持し、質問本文や `RunResult` を保持しない。

### Prompt / schema tests

- Gemini schemaのrequired / enum / nullableが `InputSafetyAgentOutput` と一致し、
  `provider_safety_filter` を含まない。
- prompt に形式検証、検索計画、回答生成の責務が混入していない。
- question と previous turn の境界タグ、section header が sanitize される。
- assistant `missing_aspects` を prompt に含めない。
- policy prompt が実行能力を高めるかという中心ルールと短い例外だけを持つ。
- 詳細なpolicy境界例をpromptへ埋め込まず、evaluation caseと分離する。
- Agent宣言のname、model、model settings、output type、response schemaが確定値と一致する。
- 固定instructions、input template、response schema変更時にmanual prompt versionを更新する契約を守る。

### Runtime / service tests

- structured `allow` / `block` responseをstrict Agent output contractで検証し、application check resultへ
  変換する。
- non-streaming prompt feedback blockを `AIProviderInputRejectedError(INPUT_BLOCKED)` に変換する。
- input `INPUT_BLOCKED` とfinish reason `SAFETY` だけを `block(provider_safety_filter)` に変換する。
- provider-neutralなsafety rejection分類がない同名reasonはblockへ変換しない。
- Agent responseから `provider_safety_filter` を返した場合はschema不正として扱い、blockへ正規化しない。
- RECITATION、空 response、not JSON、not object、validation failure を typed failure にする。
- provider exception を既存 Gemini error translator で変換する。
- 1 check につき provider call が1回で、repair retry しない。
- allowはallow metricだけを記録し、`agent_input_safety_blocked` logを出さない。
- blockはblock metricと `agent_input_safety_blocked` logを各1回記録する。
- checker failureはfailed metricとPII-free operational logを記録して元例外を伝播する。
- structured logにquestion、previous turn、raw responseが存在しない。
- `build_answering_runner()` が `INPUT_SAFETY_AGENT` を使うInputSafetyServiceをRunnerへ注入する。
- Runnerの構築だけではsafety用runtime scopeや通常回答用resourceをactivateしない。

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

### Runner tests

- InputSafetyCheckerが `AnsweringRunner.run()` 内で最初に1回だけ呼ばれる。
- historyなしでは `previous_turn=None` をcheckerへ渡す。
- completed previous runでは直前user questionと対応するassistant answerだけを渡す。
- failed previous runでは直前user questionを渡し、さらに前のassistant answerを混ぜない。
- policy blocked previous runでも直前user questionを渡し、さらに前のassistant answerを混ぜない。
- current / previous user / previous assistantを各最大1,000文字に制限する。
- allowでは `safety_check.is_blocked is False` を確認後、QuestionContextService以降を実行する。
- blockでは `safety_check.is_blocked is True` を確認し、同じreasonの `InputSafetyBlocked` をraiseする。
- block判定に `block_reason is not None` を使わないことをsource-level contractで固定する。
- blockではQuestionContextService、phases factory、Planner、retrieval、answer Agentが0 callである。
- checker failureでも同じ後続が0 callで、元のtyped exceptionを伝播する。
- `InputSafetyBlocked` が `agent_answering_run` spanをERRORにせず、exception eventを作らない。
- `RunResult` contractとanswered pathの戻り値が変わらない。

### Repository tests

- `mark_policy_blocked()` が現在のrunning attemptだけを `policy_blocked` へ遷移する。
- 遷移後はassistant message ID、error code、progress stageがnullで、completed atが設定される。
- message、source、thread updated atを変更しない。
- terminal / cancelled / stale attemptではFalseを返して既存状態を上書きしない。
- `policy_blocked` をterminal statusとして再acquire、cancel、stale sweepの対象外にする。
- policy blocked後のcancelは `ALREADY_POLICY_BLOCKED` として204を返し、quotaをreleaseしない。
- `completed ⇔ assistant_message_id is not null` と `failed ⇔ error_code is not null` を維持する。

### Migration tests

- upgrade後に `policy_blocked` rowを作成できる。
- `policy_blocked + assistant_message_id` と `policy_blocked + error_code` をDB constraintが拒否する。
- 既存queued、running、completed、failed rowを変更しない。
- downgrade前に `policy_blocked` rowが存在する場合は明示的なprecondition errorで中止し、別statusへ
  暗黙変換またはrow削除を行わない。

### Worker tests

- workerがsemantic executionとして `AnsweringRunner.run()` だけを1回呼ぶ既存境界を維持する。
- `InputSafetyBlocked` をgeneric errorより先にcatchする。
- blockはassistant messageを構築せず `mark_policy_blocked()` へ現在のattempt epochを渡す。
- DB commit後だけ `policy_blocked` terminalをpublishしてreturnする。
- blockを `RunResult`、`AnswerQuestionResult`、`failed`へ変換しない。
- provider `SAFETY` block も同じ短絡経路を通る。
- checker failure は後続0 callで generation_unavailable terminal にする。
- block branch では progress stage、answer delta、question.resolved event を publish しない。
- cancellation / stale attempt の既存 fencing と idempotency を壊さない。
- `import app.main` が google SDK を eager import しない既存契約を維持する。

### API / live transport tests

- PydanticとOpenAPIのrun status unionに `policy_blocked` が含まれる。
- run pollingとthread detailが `policy_blocked`、`errorCode=null`、`progressStage=null`を返す。
- policy blockedのrun pollingはRedis recent eventを読まず `recentEvents=[]`を返す。
- thread detailはblocked user messageの後にassistant messageを追加しない。
- terminal event `{attemptEpoch, status: "policy_blocked"}` をparse / replayでき、errorCodeを要求しない。
- terminal eventはcompleted / policy_blockedのerrorCodeを拒否し、Redis payloadからnull keyも省く。
- `policy_blocked` terminalはDB commit前にpublishされない。
- `/gen-types`後のgenerated unionに `policy_blocked` が含まれる。

### Frontend tests

- SSE、polling、thread初回表示の各経路で同じpolicy noticeを表示する。
- policy blockedでlive draftを破棄し、completed finalizingやfailed error表示へ入らない。
- assistant bubble、source panel、missing aspects、回答完了announcementを表示しない。
- 同じrunのSSE / polling重複でnoticeを再announcementしない。
- unknown terminal statusの既存fallbackを維持する。

## Work plan

1. `input_safety` contract、`is_blocked`、block reason、validator、port、正常停止例外を追加する。
2. Prompt / schema / `INPUT_SAFETY_AGENT` 宣言を共通Agent contractで追加する。
3. 共通AgentRuntimeを1回呼ぶInputSafetyService、block-only structured log、outcome metricを追加する。
4. non-streaming Gemini runtimeのprompt feedback block分類を追加する。
5. AnsweringRunnerの先頭phaseへcheckerを配線し、blockを `InputSafetyBlocked` で短絡する。
6. `_answering_run_span()` がblock停止をERROR扱いせずworkerへ再送出するようにする。
7. `AgentRunStatus`、DB CHECK、Pydantic schema、projection、SSE terminalへ `policy_blocked` を追加する。
8. attempt epochでfenceした `mark_policy_blocked()` とmigration / repository testsを追加する。
9. workerがblock停止をcatchしてrunだけをpolicy blockedへ遷移し、checker failureをfailedへ分類する。
10. `/gen-types`でfrontend typeを同期し、live reducer / controller / turn noticeを追加する。
11. contract、prompt、runtime、service、Runner、worker、repository、API、frontend testを追加する。
12. backend / frontendの`/check`でformat、lint、types、testsを検証する。

## Done

- 全research runが `AnsweringRunner.run()` の最初のphaseでInputSafetyCheckerを1回通る。
- `input_safety_result` は `allow | block` 以外を受け付けない。
- applicationのblock判定が `safety_check.is_blocked` として明示され、`block_reason`は判定後にだけ使う。
- blockは `InputSafetyBlocked` としてRunnerからworkerへ伝わり、`RunResult` contractに混入しない。
- `block` はassistant messageを持たない `policy_blocked` runとして終端する。
- DB、polling API、thread detail、SSE、frontend typeが同じ `policy_blocked` statusを使う。
- frontendがblocked turnへ固定policy noticeを表示し、assistant answerとして扱わない。
- block branchでQuestion Context Agent、Planner、検索、通常回答生成が0 callであることをtestが証明する。
- safety 判定が失敗した場合も後続へ進まない。
- blockだけが拒否structured logを生成し、allowは成功監査eventを生成しない。
- metric から allow / block / failed の件数と拒否率を算出できる。
- audit / log / metric に question、previous turn、raw response が含まれない。
- `block_reason`、質問、履歴、provider responseをDB、API、Redis、frontendへ公開しない。
- status CHECK以外のDB schema、API field、Redis event kindを追加しない。
- 新規 dependency、環境変数、認証・認可変更がない。
- backend / frontendの`/check`がgreenである。

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
