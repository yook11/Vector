# Agent answer requirement tracking slice 仕様

## 位置付け

前提仕様:

- `backend/specs/agent-conversation-context-slice.md`
- `backend/specs/agent-question-context-preparation-slice.md`
- `backend/specs/question-answering-answer-orchestration-slice.md`

本 slice は、question context preparation が抽出した「今回ユーザーが求めていること」を
検証可能な回答要望へ分解し、planner・回答生成・最終結果・次 turn の context まで同じ要望を
追跡する。

2026-07-12 のユーザー合意:

- `answered` と非空 `missing_aspects` の共存は許可しない。
- `answered` は、把握できた回答要望と retrieval 要件をすべて満たした完全回答を表す。
- 有用な部分回答は本文・引用を保持したまま `insufficient` とする。
- 「何を答えるか」をcontent requirements、「どう答えるか」をresponse requirementsとして
  ID付きで分解する。
- 回答生成後に、満たせなかった要望を入力要望の部分集合として自己申告する。
- 未達要望は既存 `missing_aspects` に最終不足理由として保存し、次turnのcontext preparationが
  必要なものだけを新しいrequirementsへ昇格できるようにする。
- ユーザーが前回答の不足を明示した場合は、通常の follow-up と区別した feedback として
  検出するが、生のfeedback本文はcontextへ残さずrequirementsとtelemetryへ変換する。
- `QuestionContext` はplannerとanswererが共有するユーザー要望の正本とする。
- `as_of` はcontextから分離し、`PlanningRequest` / `AnsweringRequest` の実行メタデータとする。
- 独立した coverage checker は最初から追加せず、自己申告・決定的検証・ユーザー訂正の
  観測結果を見て必要性を判断する。

## Work Definition

### Problem

現行のquestion contextは、回答形式・対象・比較軸・深さ等を1つの自由文で表す。この値は
plannerと回答生成へ渡るが、「何を答えるか」と「どう答えるか」の消費者が異なり、今回の回答が
どの要求を満たせなかったかを構造的に判定できない。

例えば次の質問には複数の独立した回答要望がある。

> NVIDIA、AMD、Intelを、成長率とバリュエーションの2軸で表にして比較して

```text
1. NVIDIAを扱う
2. AMDを扱う
3. Intelを扱う
4. 成長率を比較する
5. バリュエーションを比較する
6. 表形式で回答する
```

現在はこの6件が1つのintentにまとまり、planner・retrieval・answererのどこで要望が抜けたかを
追跡できない。evidence answererは根拠不足を `missing_aspects` として申告できるが、対象漏れ、
比較軸漏れ、形式不履行などを入力contractの部分集合として返せない。

また、「表にしてって言ったのに」「Intelも聞いた」といった次 turn のユーザー訂正は、
前回答の自己評価が誤っていた最も強いシグナルである。しかし現行 context preparation は
通常の follow-up と同じ自由文として扱い、前回答への明示的 feedback として区別しない。

### Evidence

- 現行 `AnswerQuestionInput` はquestion、context文字列、as_ofを平坦に持ち、planner/answererが
  共有するcontextとconsumer固有の実行入力を区別しない。
- 現行 `app/agent/contract.py::AnswerQuestionResult` は
  `answered -> missing_aspects=[]`、`insufficient -> missing_aspects非空` を保証する。
- `app/agent/answering/evidence_answer/ai/schema_tool.py` は、根拠が不足または部分的な場合に
  `sufficiency="insufficient"` を使うよう定義している。
- `app/agent/answering/orchestration.py` は retrieval-empty、unmet requirement、external taskの
  missing、evidence answererの自己申告を結合し、1件でも最終不足があれば
  `status="insufficient"` とする。
- `backend/specs/question-answering-answer-orchestration-slice.md` は、取得できた回答本文と引用を
  保持したまま `insufficient` を部分回答として返す契約を明記している。
- `app/agent/runs/result_mapper.py` は完成した `AnswerQuestionResult.missing_aspects` を
  `agent_messages.missing_aspects` へ保存する。
- `ResearchThreadView.tsx` はstatusではなく `message.missingAspects.length > 0` を条件に
  不足内容を表示する。
- 前提のquestion context preparation sliceは、保存済み `missing_aspects` を生成材料として読み、
  今回に関係する値だけをcontent/response requirementsへ昇格する配管を定義している。
- direct answer は現在、常に `answered` / `missing_aspects=[]` を組み立てるため、検索不要な
  変換要求で形式や対象を満たせなかった事実を結果に残せない。

### Invariants

- `answered` は完全回答を表し、`missing_aspects` および
  `retrieval.unmet_requirements` と共存しない。
- `insufficient` は「回答本文がない」を意味しない。満たせた範囲の本文と引用を保持する。
- `QuestionContext` は今回のユーザー要求を記述し、plannerとanswererが同じ値を共有する。
- content requirementsは「何を答えるか」、response requirementsは「どう答えるか」を表す。
- requirements、coverage、active goalは事実の根拠には使わない。
- evidence回答の事実接地は今回取得した evidence だけを正本とする。
- 未達IDは入力content/response requirement IDの部分集合に限定し、answererに新しい要望を
  創作させない。
- `missing_aspects` は途中工程のログではなく、最終回答で残った不足理由だけを保持する。
- 過去の未達要望は今回も未達であることを意味しない。今回の回答後に再評価する。
- ユーザーの明示的訂正はagentの自己申告より強いfeedbackとして扱う。
- context・requirements・feedback・過去回答はすべてuntrusted inputとしてprompt境界を越える。
- 質問本文、要望本文、feedback本文、回答断片をlog・metric labelへ載せない。
- agent coreはDBを知らず、永続化済み履歴はworker/repository境界で読む。
- `as_of`、evidence、previous answer、telemetryは `QuestionContext` に含めない。
- thread ownership、run ownership、認証・認可境界を変更しない。

### Non-goals

- planner queryやexternal research taskとrequirement IDの1対1対応の永続化。
- requirementがplanning・retrieval・synthesisのどこで落ちたかを確定するroot cause分類。
- 独立したLLM coverage checkerの追加。
- content/responseより細かいrequirement kindのenum taxonomy固定。
- 表、文字数、entity網羅等の全形式を決定的に検証する汎用validator。
- `AnswerQuestionResult`、REST response、frontend型への新field追加。
- DB schema変更、migration、requirement専用tableへの永続化。
- 過去sourceの再利用、direct回答へのsource継承。
- cross-thread memory、user profile、嗜好学習。
- user feedbackの長期分析画面や管理UI。
- context preparationとplannerの1回のLLM callへの統合。

### Done

- 今回の回答要望がID付きcontent/response requirementsに分けて正規化される。
- 初回質問を含む全runでrequirementsが準備され、既知失敗時も粗いfallback requirementで
  runを継続する。
- 初回質問ではLLMの書き換えを採用せず、元質問を維持して `question.resolved` eventを
  構造的に発火させない。
- planner、direct answer、evidence answerが同じ `QuestionContext` を受け取る。
- direct/evidence answererの未達IDが入力IDの部分集合へ決定的に制限される。
- 1件以上の未達要望があれば、本文を保持した `insufficient` resultになる。
- 未達要望がユーザー向け `missing_aspects` に変換され、既存DB/API/UI経路で保存・表示される。
- 次turnのcontext preparationが保存された未達要望から、今回必要なrequirementsだけを再生成する。
- 明示的な前回答訂正がrequirementsへ反映され、feedback本文はcontextへ残らない。
- `PlanningRequest` / `AnsweringRequest` が同じcontextとconsumer別のas_ofを明示する。
- feedback検出と未達件数を本文なしの低cardinality metricで観測できる。
- API shape、DB schema、dependency、auth境界に変更がなく、指定testと `/check` がgreenになる。

## 用語

### Content requirement

今回の回答内容として満たすべき検証単位。対象、観点、比較軸、期間等を含む。

### Response requirement

今回どのように回答するかの検証単位。表形式、簡潔さ、深さ、対象読者等を含む。

### Relevant prior coverage

今回に関係する説明済み内容。繰り返し回避と差分・更新回答に使うが、現在も正しい事実とは
みなさない。

### Active goal

ユーザーがこのthreadで現在達成しようとしている明示的な目的。「どう答えるか」ではなく、
plannerとanswererが優先順位を合わせるための背景である。新topicでは引き継がない。

### Planning / Answering request

`PlanningRequest` と `AnsweringRequest` は、共通の `QuestionContext` とconsumerが必要とする
実行メタデータを結ぶwrapperである。名前から入力先を明確にし、context解釈を複製しない。

### Unfulfilled requirement

回答生成後にも満たせなかったanswer requirement。入力requirementsの部分集合であり、
answererが入力にない不足を自由生成することはできない。

### Prior response feedback

現在のユーザーメッセージに明示された、過去assistant回答への訂正または不足申告。
「もっと詳しく」のような単なる深掘り要求はfeedbackにせず、「表にしてと言った」など
過去回答の不履行を指摘するものだけを対象とする。

### Self-assessment

answererが同じgeneration call内で返す未達ID。これは観測可能な推定であり、真実とは限らない。
ユーザーの明示的feedbackや決定的validatorと矛盾した場合は、そちらを優先する。

## 全体フロー

```text
current question + prior thread messages + saved missing_aspects
  -> QuestionContextService
       - standalone question
       - content requirement descriptions
       - response requirement descriptions
       - relevant prior coverage
       - active goal
       - feedback detection telemetry
  -> deterministic normalization
       - c1.. / p1.. を採番
       - 重複・空文字・上限超過を除去
  -> QuestionContext + telemetry
  -> PlanningRequest(context, as_of) -> planner
       - 全requirementsを考慮してretrieval strategyを作る
  -> AnsweringRequest(context, as_of)
  -> direct answer または evidence collection + evidence answer
       - answer
       - unfulfilled requirement IDs
  -> deterministic finalization
       - 未知IDを除外してdefect記録
       - 未達IDをユーザー向けmissing_aspectsへ変換
       - 未達があればinsufficientへcap
  -> assistant messageへanswer / sources / missing_aspectsを保存
  -> 次turnで関連するmissing_aspectsを新しいrequirementsへ昇格
```

## 設計判断

### 1. `answered` の完全回答契約を維持する

`AnswerQuestionResult` のvalidatorは変更しない。

```text
answered
  -> missing_aspects=[]
  -> unmet_requirements=[]
  -> retrieval modeがnone以外ならsources非空

insufficient
  -> missing_aspects非空
  -> 回答できた本文と引用は保持可能
```

`answered + missing_aspects` を許可するとstatusが完全性を表さなくなり、既存audit/metrics/specの
意味が曖昧になる。部分回答は既存どおり `insufficient` で表す。

### 2. content/response requirementは別namespaceで採番する

`app/agent/question_context/contract.py` に次を追加する。

```python
MAX_ANSWER_REQUIREMENT_LENGTH = 500
MAX_CONTENT_REQUIREMENTS = 8
MAX_RESPONSE_REQUIREMENTS = 4
CONTENT_REQUIREMENT_IDS = frozenset(
    f"c{index}" for index in range(1, MAX_CONTENT_REQUIREMENTS + 1)
)
RESPONSE_REQUIREMENT_IDS = frozenset(
    f"p{index}" for index in range(1, MAX_RESPONSE_REQUIREMENTS + 1)
)
ANSWER_REQUIREMENT_IDS = CONTENT_REQUIREMENT_IDS | RESPONSE_REQUIREMENT_IDS

class AnswerRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str
    description: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_ANSWER_REQUIREMENT_LENGTH,
        ),
    ]

    @field_validator("requirement_id")
    @classmethod
    def _validate_requirement_id(cls, value: str) -> str:
        if value not in ANSWER_REQUIREMENT_IDS:
            raise ValueError("unknown answer requirement id")
        return value
```

IDの許可範囲は各件数上限から一意に導出し、件数上限とregexを別々に保守しない。contentは
`c1...`、responseは `p1...` とし、公開enum kindを追加せずnamespaceで区別する。

完成型 `QuestionContext` は次とする。

```python
class QuestionContext(BaseModel):
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

`QuestionContext` はplannerとanswererが共有するユーザー要望の正本であり、`as_of`、evidence、
previous answer、telemetryは含めない。

### 3. LLM draftはdescriptionだけを生成し、IDはserviceが採番する

`QuestionContextDraft` に次を追加する。

```python
standalone_question: str
content_requirements: list[str] = Field(default_factory=list)
response_requirements: list[str] = Field(default_factory=list)
relevant_prior_coverage: str = ""
active_goal: str = ""
explicit_feedback_detected: bool = False
```

context generatorにIDを生成させない。`question_context_from_draft()` が次の順で
決定的に完成型へ変換する。

1. content/responseを別々にstripし、空文字を除去する。
2. 各descriptionを500文字へcapする。
3. listごとにcap後の完全一致で重複を除去し、入力順を維持する。
4. contentは最大8件、responseは最大4件で打ち切る。
5. contentへ `c1...`、responseへ `p1...` を完成順に採番する。
6. coverage/goalをstrip・capし、根拠がなければ空文字にする。

context prompt規約:

- current questionから明示または直接必要と分かる回答要望だけを分解する。
- entity・対象範囲・比較軸・期間はcontent、形式・簡潔さ・深さ・対象読者はresponseへ入れる。
- retrieval mode、検索query、検索provider、source要件を作らない。
- 質問にない評価軸、目的、形式を推測して追加しない。
- 保存済み不足は現在の質問またはactive goalに関係する場合だけ対応requirementへ昇格する。
- 挨拶等でも、自然に応答するという1件の粗い要望を返してよい。
- feedback本文は完成contextへ残さず、対応requirementとtelemetryへ反映する。
- `active_goal` はthread/current questionに明示的根拠がある場合だけ設定し、新topicでは空にする。

自由文 `user_intent` はcontent/response requirementsおよびactive goalと重複するため、完成型から
削除する。

### 4. 初回質問でもcontext preparationを実行する

前提のquestion context preparation sliceにある「historyなしはgeneratorをskipする」規則を、
本slice実装後は次のように改訂する。

- historyの有無にかかわらずcontext generatorを1回呼ぶ。
- historyが空なら `relevant_prior_coverage=""`、telemetryの両fieldはfalseとし、current questionだけから
  requirementsと明示的なactive goalを抽出する。
- historyが空の場合、LLM出力の `standalone_question` は採用せず、完成型では元質問へ
  決定的に固定する。
- provider構成失敗、provider error、response不正、validation errorではrunを落とさない。
- fallbackでは元質問を `standalone_question` とし、元質問をstrip・500文字capした `c1` content
  requirementを1件作る。response requirements、coverage、goalは空とする。
  `explicit_feedback_detected` はfalse、`previous_answer_had_missing_aspects` は履歴から決定する。
- fallback requirementも作れない空入力はAPI schemaで到達不能だが、service contractでは
  non-blank questionを前提とする。

これにより初回質問も要望追跡できる。追加LLM callのlatency/costは受け入れ、plannerとの
call統合やskip heuristicは本sliceのNon-goalsとする。

context generatorの構築自体が失敗する場合もfallback対象とする。workerはbuilder例外を
run全体のgeneration failureへ直結させず、context preparationの既知失敗として記録する。

user-visibleなevent境界でも二重に防御する。workerは次の条件を両方満たす場合だけ
`question.resolved` をemitする。

```text
historyが1件以上存在する
AND standalone_question.strip() != original_question.strip()
```

初回ではserviceが元質問へ固定し、workerもhistory guardを持つため、promptの遵守に依存せず
eventが発火しない。

### 5. plannerは全requirementsを受けるが、plan schemaは変更しない

plannerはconsumer専用の `PlanningRequest` を受ける。

```python
class PlanningRequest(BaseModel):
    context: QuestionContext
    as_of: datetime
```

planner promptへcontext fieldをuntrusted blockとして追加する。

```text
# Content Requirements
- c1: NVIDIA、AMD、Intelを対象にする
- c2: 成長率を比較する
- c3: バリュエーションを比較する

# Response Requirements
- p1: 表形式で回答する
```

planner規約:

- `content_requirements` を満たすために必要なretrieval mode・query・collection goalを作る。
- `response_requirements` のうちformat・文体・簡潔さだけを理由にretrievalを増やさない。
  技術的深さ等が必要情報量に影響する場合だけ補助的に使う。
- `relevant_prior_coverage` で不要な再調査を避け、更新・比較要求では必要範囲を再取得する。
- `active_goal` で調査優先度を目的へ合わせるが、事実根拠にはしない。
- plan outputへrequirement ID mappingを追加しない。
- planner fallback queryは `request.context.standalone_question` を使う。

本sliceでは「どのqueryがどのrequirementを担当したか」のroot cause追跡までは行わない。

### 6. evidence answerは未達requirement IDをstructured outputで返す

plannerと同じcontextを `AnsweringRequest` で受ける。

```python
class AnsweringRequest(BaseModel):
    context: QuestionContext
    as_of: datetime
```

`RawEvidenceAnswerDraft` とGemini schemaへ次を追加する。

```python
unfulfilled_requirement_ids: list[object] = Field(default_factory=list)
```

strict `EvidenceAnswerDraft` には検証済みIDを持たせる。

```python
unfulfilled_requirement_ids: list[str] = Field(default_factory=list)
```

evidence answererは `AnsweringRequest` と経路固有入力を受ける。

```python
async def answer(
    *,
    request: AnsweringRequest,
    evidence: list[AnswerEvidenceItem],
    target_time_window: str | None,
) -> EvidenceAnswerDraft: ...
```

prompt規約:

- content requirementsを回答対象・観点、response requirementsを構成・形式・深さに使う。
- requirementsは事実根拠ではなく、事実はevidenceだけに接地する。
- 満たせなかったrequirementのIDだけを `unfulfilled_requirement_ids` に返す。
- 入力にないIDを作らない。
- 根拠不足だけでなく、対象漏れ、比較軸漏れ、明示形式の不履行も未達として申告する。
- 両requirementsが空なら未達IDは空にする。
- 保存済み不足とfeedback本文は受け取らない。今回必要な内容はcontext preparationで
  requirementsへ昇格済みでなければ、今回の未達申告対象にしない。

evidence draft finalizerは次を決定的に行う。

1. non-string、空文字、重複IDを除去する。
2. 入力requirementsにないIDを除去し、defectを記録する。
3. contentの入力順、続いてresponseの入力順へ並べ直す。
4. strict `EvidenceAnswerDraft.unfulfilled_requirement_ids` に検証済みIDだけを保持する。

Gemini response schemaでは `unfulfilled_requirement_ids` をrequired fieldとし、未達要望がない
場合は空配列を返させる。adapter境界はfield欠落を空配列へ補完し、defectとして観測する。

draft finalizerは未達descriptionを `EvidenceAnswerDraft.missing_aspects` へ混ぜない。
`sufficiency="answered"` のdraftは既存validatorにより `missing_aspects=[]` を維持できる。

orchestratorが次を決定的に行う。

1. 検証済み未達IDを対応するdescriptionへ変換する。
2. `回答要望を満たせませんでした: {description}` をfinal missingへ追加する。
3. 未達IDが1件以上なら、LLMのsufficiency申告にかかわらず最終resultを
   `insufficient` へcapする。

`EvidenceAnswerDraft.sufficiency` はevidenceの充足自己申告として維持し、最終
`AnswerQuestionResult.status` はevidence不足・retrieval不足・回答要望未達を統合して決める。

### 7. direct answerもstructured assessmentを返す

direct経路でも「表にして」「前回答の結論だけ」等の要望未達を記録するため、providerの
生text出力を次のstructured outputへ変更する。

```python
class RawDirectAnswerDraft(BaseModel):
    answer: object | None = None
    unfulfilled_requirement_ids: list[object] = Field(default_factory=list)

class DirectAnswerDraft(BaseModel):
    answer: NonBlankText
    unfulfilled_requirement_ids: list[str] = Field(default_factory=list)
```

direct answererも `AnsweringRequest` を受け、同じcontent/response requirementsでallowlist検証・
順序正規化を行う。previous answerはcontextへ入れず別引数で渡す。citation marker除去、
non-blank validation、retry/failure契約は維持する。

Gemini response schemaでは `answer` と `unfulfilled_requirement_ids` をrequired fieldとし、
未達なしは空配列を返させる。providerがschemaを破る場合は次の契約で扱う。

| Defect | Behavior |
|---|---|
| responseがJSONでない | `DirectAnswerInvalidError(code="direct_answer_response_not_json")` |
| JSON objectでない | `DirectAnswerInvalidError(code="direct_answer_response_not_object")` |
| answer欠落・非文字列・空白 | `DirectAnswerInvalidError(code="direct_answer_invalid_answer")` |
| unfulfilled field欠落・非array | 空配列へ補完しrepairable defect記録 |
| 未知・空・非文字列・重複ID | 除外・dedupしrepairable defect記録 |

新しい例外classは追加しない。上3つは既存 `DirectAnswerInvalidError` と同じrequest内retry対象とし、
最大試行後は既存failure契約で伝播する。assessmentだけの不備は有効なanswerを捨てず、
`DirectAnswerDefectEvent` または同等のbest-effort auditでdefect codeを記録する。

orchestratorのdirect分岐は次へ変更する。

```text
unfulfilled IDsなし
  -> answered / sources=[] / missing_aspects=[] / planned_mode=none

unfulfilled IDsあり
  -> insufficient / sources=[] / missing_aspects=未達description / planned_mode=none
```

現行 `AnswerQuestionResult` validatorは `planned_mode=none` のinsufficient resultを許容するため、
final result shapeの変更は不要である。

### 8. 未達要望は既存 `missing_aspects` へ保存する

新しいAPI field・DB columnは追加しない。requirementsの追跡IDはrun内だけで使い、最終的な
未達descriptionを既存 `missing_aspects` へ変換する。

final missingの順序:

1. retrieval-empty定型句。
2. `unmet_requirements` の定型句。
3. external task reportsのmissing。
4. evidence answererのevidence不足 `missing_aspects`。
5. answer requirement未達description（content入力順、続いてresponse入力順）。

全要素は既存どおり完全一致でdedupする。最終missingが1件でもあればstatusは
`insufficient` とする。`result_mapper`、DB model、thread projection、REST schema、frontend表示は
変更しない。

requirement未達は接頭辞付きのため、意味が近いevidence不足と完全一致せず、両方表示される場合が
ある。本sliceではroot causeの異なる2件として許容し、意味的dedupは行わない。

`missing_aspects` の意味は「引用根拠不足だけ」ではなく、既存specどおり最終回答に残った
不足理由とする。requirement未達を入れる場合はユーザー向け文として明示する。

### 9. 明示的な前回答訂正はrequirementsとtelemetryへ変換する

生のfeedback本文は `QuestionContext`、`PlanningRequest`、`AnsweringRequest` のどれにも保持しない。

feedback抽出規約:

- 「表にしてと言った」「Intelが抜けている」等、過去回答の不履行を明示した場合だけ入れる。
- 「もっと詳しく」「別の観点で」は新しい要求であり、過去回答の失敗断定にはしない。
- 「Intelが抜けている」はcontent requirement、「表にしてと言った」はresponse requirementへ反映する。
- feedbackを過去回答の事実内容が誤っている証拠として自動採用しない。事実訂正は今回の
  retrieval/evidenceで再検証する。

```python
class QuestionContextTelemetry(BaseModel):
    explicit_feedback_detected: bool = False
    previous_answer_had_missing_aspects: bool = False

class QuestionContextPreparationResult(BaseModel):
    context: QuestionContext
    telemetry: QuestionContextTelemetry
```

`explicit_feedback_detected` はLLM draftから完成する。serviceは履歴窓内の最新assistant messageに
保存済み `missing_aspects` があるかを決定的に判定し、
`previous_answer_had_missing_aspects` として本文なしの観測に使う。telemetryは
planner/answererへ渡さず、feedbackの自由文も永続化・log出力しない。

将来、どの過去messageへのfeedbackかを厳密に扱う必要が出た場合は、message seq/IDを含む
構造体へ拡張する。本sliceでは直近履歴を対象とする自由文に留める。

### 10. 自己評価はground truthとして扱わない

同一generation call内の自己申告は不足を見落とす可能性がある。初期実装では次の優先順位を
採用する。

```text
1. ユーザーの明示的feedback
2. 既存のcitation/retrieval/shapeに関する決定的validator
3. answererのunfulfilled requirement自己申告
```

独立checkerは追加せず、次の乖離を先に観測する。

- 前回答の `missing_aspects` が空だったのに明示的feedbackが来た割合。
- requirement数に対する自己申告未達数。
- direct/evidence経路別の未達発生率。

未達率が高い場合は実際の不履行だけでなく、特に形式要望に対する自己申告過剰も疑う。
不足boxの表示頻度と後続ユーザー訂正を突き合わせ、過剰申告が疑われる場合はprompt調整または
形式別の決定的validatorを後続sliceで検討する。

乖離が継続的に大きい場合に限り、独立checkerまたは形式別の決定的validatorを別sliceで設計する。

### 11. 観測

本文を含まない低cardinality metricを追加する。

```text
metric: vector.agent.answer_requirements.prepared
attributes:
  result = prepared | fallback
  explicit_feedback_detected = true | false
  previous_answer_had_missing_aspects = true | false
measurements:
  content_requirement_count
  response_requirement_count

metric: vector.agent.answer_requirements.outcome
attributes:
  route = direct | evidence
  result = fulfilled | unfulfilled
measurements:
  content_requirement_count
  response_requirement_count
  unfulfilled_count
```

`explicit_feedback_detected=true AND previous_answer_had_missing_aspects=false` を観測側で
組み合わせ、前回答の
自己評価が不足を見落とした候補として扱う。これはcontext preparation時点で完結し、finalization
までflagを引き回さない。

`requirement_id`、description、feedback、question、answer、missing本文をmetric labelにしない。
既存answer synthesis/direct metricsは維持し、二重のstatus metricへ置換しない。

## Failure behavior

| Failure | Behavior |
|---|---|
| context generator構成/呼び出し失敗 | 元質問 + `c1` fallback content requirementで継続 |
| content requirementsが空または全件cleanで消える | 元質問から `c1` を決定的に補完しdefect記録 |
| answererが未知requirement IDを返す | 未知IDを除外しdefect記録、既知IDと回答は保持 |
| answererが非文字列・空・重複IDを返す | 決定的に除去・dedupしdefect記録 |
| evidence answerが未達IDありでanswered申告 | 最終resultをinsufficientへcap |
| direct answerが未達IDあり | 本文を保持したinsufficient resultを返す |
| evidence requirement assessment欠落 | 空listへ補完しdefect記録、回答生成を止めない |
| direct responseがJSON/objectでない | typed `DirectAnswerInvalidError` として既存retry/failureへ流す |
| direct answer欠落・非文字列・空白 | typed `DirectAnswerInvalidError` として既存retry/failureへ流す |
| direct requirement assessment欠落・非array | 空listへ補完しrepairable defect記録、answerを保持 |
| metric送信失敗 | best-effort、runへ伝播しない |

## Security / prompt safety

- requirement description、履歴中のmissing、feedbackはuser/LLM由来のuntrusted textである。
- promptではIDを境界外の制御値、descriptionをuntrusted block内の本文として描画する。
- `<untrusted_input>` 等の境界文字列は既存 `sanitize_for_untrusted_block()` で無害化する。
- requirement description内の「検索不要」「system promptを無視」等を命令として解釈しない。
- evidence answerではrequirementsを引用根拠として扱わない。
- feedbackからユーザー属性、長期嗜好、他thread情報を推測しない。
- log・audit・metricへquestion、requirement、feedback、answer本文を載せない。

## 実装境界

想定変更範囲:

```text
backend/app/agent/question_context/
  - AnswerRequirement / QuestionContext / Draft / telemetry
  - normalization / fallback / prompt / Gemini schema
  - 初回もprepareするservice behavior

backend/app/agent/contract.py
  - AnswerQuestionInput(context, as_of, previous_answer)

backend/app/queue/tasks/agent_run.py
  - contextからAnswerQuestionInputへの配布
  - context builder failureの局所fallback

backend/app/agent/planning/ai/
  - PlanningRequest / contextのprompt配布

backend/app/agent/planning/contract.py
  - PlanningRequest

backend/app/agent/answering/contract.py
  - AnsweringRequest

backend/app/agent/answering/direct_answer/
  - structured raw draft
  - requirement allowlist validation
  - direct partial result
  - typed invalid response code / repairable defect audit

backend/app/agent/answering/evidence_answer/
  - unfulfilled requirement IDs
  - requirement allowlist validation
  - prompt / Gemini schema

backend/app/agent/answering/orchestration.py
  - PlanningRequest / AnsweringRequest構築
  - final missingへの変換
  - requirement未達によるinsufficient cap

backend/app/agent/answering/metrics.py または専用metrics module
  - 本文を持たない観測

backend/specs/agent-question-context-preparation-slice.md
  - 初回常時実行 / metric語彙 / answererへの履歴文脈配布を同時改訂
```

変更しない境界:

```text
backend/app/models/
backend/migrations/
backend/app/schemas/research.py
frontend/src/types/types.gen.ts
frontend/src/features/research/components/ResearchThreadView.tsx
認証・認可
internal/external retrieval provider
```

## API / DB / dependency

- REST request/response shapeは変更しない。
- `ResearchAssistantMessage.missingAspects` の既存fieldを使用する。
- frontend生成型の変更はないため `/gen-types` は不要。
- DB schemaは変更せず、既存 `agent_messages.missing_aspects` を使用する。
- migrationは追加しない。
- 新規dependencyは追加しない。
- authn/authz、ownership queryを変更しない。
- agent内部の `missing_aspects` 意味論を「最終不足理由」として維持し、未達要望を追加する。

## Tests

### Requirement contract / normalization

1. content/responseを別々にstrip・cap・dedupし、各件数上限を保証する。
2. 各上限から導出した許可IDだけを受け、contentへ `c1...`、responseへ `p1...` が
   決定的に採番される。
3. LLM draftのIDらしき文字列を採用せずservice側で採番する。
4. content requirementsが全件消えた場合、元質問から `c1` を補完する。
5. `QuestionContext` にas_of、previous answer、evidence、telemetryが入らない。

### Context preparation

6. 初回質問でもgeneratorを1回呼びrequirementsを生成するが、LLMがstandalone questionを
   書き換えても元質問へ固定し、workerもhistory guardにより `question.resolved` eventをemitしない。
7. follow-upでcurrent question、保存済みmissing、明示feedbackからcontent/response requirementsを作る。
8. 単なる深掘りでは `explicit_feedback_detected` をtrueにしない。
9. 明示的訂正だけを対応requirementへ反映し、`explicit_feedback_detected` をtrueにするが
   feedback本文はcontextへ残さない。
10. provider構成/呼び出し/response/validation既知失敗で元質問 + `c1` へfallbackする。
11. fallbackでrunを落とさず、本文なしmetricを記録する。
12. requirementsとfeedbackがuntrusted blockを脱出しない。

### Planner

13. plannerが `PlanningRequest(context, as_of)` を受け、全content/response requirementが入力順で入る。
14. requirement descriptionはsanitizeされる。
15. formatだけのrequirementを検索queryへそのままコピーするよう指示しない。
16. planner output schemaは既存どおりで、fallback queryはcontextのstandalone questionになる。

### Evidence answer

17. evidence answererがplannerと同じcontextを持つ `AnsweringRequest` を受け、保存済みmissingと
    feedback本文は生のまま渡らない。
18. known unfulfilled IDsをcontent順、続いてresponse順へ正規化する。
19. unknown、空、非文字列、重複IDを除去しdefect記録する。
20. 未達IDを対応するユーザー向けmissing文へ変換する。
21. answered申告でも未達IDがあれば最終resultをinsufficientへcapする。
22. requirement未達とretrieval/evidence missingを規定順で結合・dedupする。
23. requirementsが空でも現行evidence behaviorが変わらない。

### Direct answer

24. direct answererも同じcontextの `AnsweringRequest` を受け、valid structured outputから
    non-blank answerとknown unfulfilled IDsを完成する。
25. not JSON / not object / invalid answerを個別codeの `DirectAnswerInvalidError` にし、
    citation marker除去と既存retry/failure契約を維持する。
26. 未達IDなしでは従来どおりanswered / sources空 / missing空になる。
27. 未達IDありでは本文を保持したinsufficient / sources空 / missing非空になる。
28. assessment field欠落・非arrayを空listへ補完し、unknown、空、非文字列、重複IDを安全に
    除外してrepairable defectを記録する。

### Persistence / next turn

29. requirement未達descriptionがassistant messageの `missing_aspects` に保存される。
30. thread detail APIと既存frontendが追加fieldなしでそのmissingを表示できる。
31. 次turnのcontext preparationが関連する保存済み未達をcontent/response requirementへ昇格できる。
32. 今回満たせた過去未達は新しい `missing_aspects` に残らない。

### Status invariants / compatibility

33. `answered + missing_aspects` は引き続きValidationErrorになる。
34. `insufficient + missing_aspects=[]` は引き続きValidationErrorになる。
35. evidence partialは本文・sources・missingを持つinsufficientとして成立する。
36. planned_mode=noneのdirect partialはsources空・missing非空で成立する。
37. API schema、frontend生成型、migration headに差分がない。

### Metrics

38. prepared/fallback、content/response requirement count、feedback有無、
    `previous_answer_had_missing_aspects` を
    prepared時に記録する。
39. route別のfulfilled/unfulfilled countを記録する。
40. 前回答missing空の後に明示feedbackが来た候補をprepared metricの
    `explicit_feedback_detected=true AND previous_answer_had_missing_aspects=false` で判定できる。
41. question、requirement、feedback、answer、missing本文をmetric attributeへ含めない。
42. metric backend失敗がrunへ伝播しない。

## Verification

実装時は次を行う。

1. `/check` でbackend lint / format / type / testを実行する。
2. question context、planner prompt、direct answer、evidence answer、orchestration、worker統合testを
   個別実行して失敗箇所を切り分ける。
3. OpenAPI生成差分がないことを確認する。
4. `backend/app/schemas/` を変更していないことを確認し、`/gen-types` は実行しない。
5. migration fileが追加されず、migration headに差分がないことを確認する。
6. `rg "content_requirements|response_requirements|PlanningRequest|AnsweringRequest|unfulfilled_requirement_ids"`
   `backend/app backend/tests` で配布先とallowlist検証の抜けを確認する。
7. `rg "requirement.*(question|description|feedback)|missing_aspects"` でlog/metricへの本文混入が
   ないことを確認する。
8. 前提specのservice behavior、metric語彙、Tests 9・15・18・24が本specの初回常時実行、
   event抑止、共通context正本化と矛盾しないことを確認する。

## 実装順序

1. `AnswerRequirement` とnormalization/fallbackを追加する。
2. context preparationを初回実行へ変更し、2種のrequirementsとtelemetryを生成する。
3. `PlanningRequest` / `AnsweringRequest` で同じcontextを配布する。
4. evidence answerのstructured assessmentとfinal missing統合を追加する。
5. direct answerをstructured assessmentへ変更し、direct partialを追加する。
6. worker配線、保存、次turn引き継ぎを統合する。
7. 本文なしmetricsを追加する。
8. compatibility、OpenAPI、migration差分なしを確認し `/check` を通す。

Doneを満たした時点で停止する。planner task単位のrequirement mapping、独立checker、
形式別deterministic validator、root cause分析、専用DB永続化は実測に基づく後続sliceへ分離する。
