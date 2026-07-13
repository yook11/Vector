# Agent evidence answer composition slice 仕様

## 位置付け

前提仕様:

- `backend/specs/agent-question-context-preparation-slice.md`
- `backend/specs/question-answering-evidence-synthesis-slice.md`
- `backend/specs/question-answering-inline-citation-slice.md`
- `backend/specs/agent-evidence-answer-draft-deltas-slice.md`
- `backend/specs/question-answering-answer-orchestration-slice.md`

本 slice は、`QuestionContext` に準備された今回のユーザー要望を evidence answerer が
回答構成へ変換し、収集済み evidence を情報源単位で列挙するのではなく、ユーザーの問いに
対する概要・論点・意味として統合して返す工程を定義する。

2026-07-13 のユーザー合意:

- 最優先は evidence の紹介ではなく、今回のユーザー要望へ答えること。
- 独立した content requirements が複数あれば、原則として要望ごとに回答箇所を分ける。
- response requirements は節にせず、回答全体の形式・深さ・対象読者等へ適用する。
- 広い調査質問では、明示 requirement が1件でも、evidence から重要なテーマを抽出して
  概要と章立てを作る。
- evidence は回答を支える根拠であり、evidence の入力順を回答構成にしない。
- 本文は既存の `answer: string` を維持し、`sections[]` は導入しない。
- 回答後に満たせなかった入力 requirement だけを内部 schema の
  `unfulfilled_requirement_ids` で自己申告する。
- 表示改善は後続とし、本 slice では backend の生成本文を先に改善する。

本 slice は、観測された根拠付きリサーチ回答を直接所有する **evidence 経路**を対象とする。
direct 経路は現在 plain-text stream であり、structured output 化にはライブ配信境界の変更が
必要になるため、同等の requirement assessment は後続 slice とする。

## Work Definition

### Problem

Question context preparation により、planner と answerer は次を共有できるようになった。

- 自己完結した質問。
- 回答すべき対象・観点・比較軸等の content requirements。
- 形式・簡潔さ・深さ・対象読者等の response requirements。
- 今回に関係する既回答。
- ユーザーが進めている明示的な調査目的。

しかし現行 evidence answer prompt は、これらを prompt に表示するだけで、要望を回答構成へ
変換する規約を持たない。主な指示は「evidence だけを引用根拠として日本語で回答する」であり、
次を定義していない。

- 冒頭で質問全体への結論や現在地を示す。
- 複数の要望を回答内で個別に扱う。
- 広い質問の evidence を重要なテーマへ束ねる。
- 各節を「要点、根拠、意味」の順で書く。
- 事実、複数根拠からの傾向、将来見通しを区別する。
- evidence 単位の列挙を避ける。

その結果、「量子コンピュータの最新動向をリサーチして」のような広い質問でも、個別の研究・
企業・政策を接続せず、収集した事実を1文ずつ並べた回答になりやすい。

また、現在の evidence answer schema は evidence の十分性と引用整合だけを返すため、対象漏れ・
比較軸漏れ・明示された回答形式の不履行を、入力 requirement のどれが未達だったかとして
構造化できない。本文が生成されても、ユーザー要望の一部を落としたことを最終結果へ反映できない。

### Evidence

- `app/agent/question_context/contract.py::QuestionContext` は、ID付き
  `content_requirements` / `response_requirements`、`relevant_prior_coverage`、
  `active_goal` を持つ。
- `app/agent/answering/orchestration.py` は同じ `QuestionContext` から
  `AnsweringRequest` を作り、evidence answererへ渡している。
- `app/agent/answering/evidence_answer/ai/prompt.py` は全 context field を表示するが、
  requirement ごとの回答構成、広い質問の標準構成、evidence のテーマ統合を指示しない。
- 同 prompt の現行規約は evidence 接地と citation を中心とし、回答編集の標準方針を持たない。
- `app/agent/answering/evidence_answer/ai/schema_tool.py` の内部 Gemini schema は
  `sufficiency` / `answer` / `cited_refs` / `missing_aspects` の4 fieldだけである。
- `app/agent/answering/evidence_answer/contract.py` の raw / strict draft も、入力 requirement の
  未達IDを持たない。
- `app/agent/answering/evidence_answer/validation.py` は citation marker、cited refs、
  missing aspectsを決定的に補正するが、requirement allowlistを受け取らない。
- `app/agent/answering/evidence_answer/json_answer_extractor.py` は root JSON の `answer` stringだけを
  増分復元するため、root field追加は可能だが、`answer`を`sections[]`へ置換すると既存の
  ライブ回答配信を壊す。
- `app/agent/answering/evidence_answer/evidence.py::AnswerEvidenceItem` は source と text のみを持ち、
  external searchの `task_index` / collection goal / `why_selected` はanswererへ渡らない。
- `app/agent/answering/orchestration.py::_missing_aspects()` は、retrieval empty、collection failure、
  external task missing、draft missingの順で不足理由を結合し、1件でもあれば最終statusを
  `insufficient`にする。
- `app/agent/answering/direct_answer/flow.py` は provider のplain-text fragmentをそのまま
  live draftへ流す。direct schema化はevidence schemaへのfield追加と同じ変更ではない。
- frontendの現行回答表示は改行を保持するがMarkdownを解釈しない。したがって本sliceでは
  Markdown描画を前提にせず、生成本文の段落・独立行の見出しで構成を改善できる。

### Invariants

- 最優先は、`QuestionContext` に記録された今回の質問と要望へ答えることである。
- `content_requirements` は回答内容の完成条件、`response_requirements` は回答全体の表現条件として
  混ぜずに扱う。
- 明示的な response requirement は標準の章立てより優先する。
- requirement、prior coverage、active goalは事実根拠ではない。事実は今回のevidenceだけに接地する。
- answererはevidenceにない事実を、引用付きの確認済み事実として書かない。
- citation markerとsourcesの既存整合契約を維持する。
- `unfulfilled_requirement_ids` は入力requirementsの部分集合であり、answererに新しい要望を
  作らせない。
- 未達IDは自己評価でありground truthではない。未知IDや壊れた要素は決定的に除外する。
- 未達IDがあっても、満たせた範囲の本文とcitationを捨てない。
- 最終 `answered` は把握した回答要望をすべて満たした状態とし、未達IDが1件でもあれば
  `insufficient`へcapする。
- `answer` は引き続き1つのstringとし、既存のincremental answer extractionを維持する。
- API、DB、frontend、認証・認可の境界を変更しない。
- 質問、requirement description、answer、evidence本文をlog・metric labelへ載せない。

### Non-goals

- `sections[]`、`outline[]`、claims配列等の新しい本文構造。
- Markdown renderer、見出しstyling、table表示等のfrontend改善。
- REST response、generated frontend types、DB schemaの変更。
- `QuestionContext`、`PlanningRequest`、planner output schema、retrieval modeの変更。
- external collection goal / task index と回答節の対応をanswererへ伝える契約変更。
- evidenceの再収集、未達判定後の再planning・再retrieval loop。
- direct answerのstructured output化と未達ID追跡。
- 独立したLLM coverage checker。
- 全ての形式要求を決定的に検証する汎用validator。
- 長文回答のためのmodel変更、token上限変更、temperature調整。
- 新しいdependency、migration、認証・認可変更。

### Done

- evidence answer promptが、質問への直接回答、複数content requirementの個別充足、広い質問の
  テーマ構成、response requirement優先、evidence統合を明示する。
- 広い調査質問の標準形が「概要、重要テーマ、現在地または意味」として定義されるが、
  evidenceや明示要望にない固定見出しは強制しない。
- Gemini内部schemaとraw / strict evidence draftが `unfulfilled_requirement_ids` を持つ。
- 未達IDが入力IDの部分集合へ決定的に制限され、入力順に正規化される。
- 未達IDが人間可読な既存 `missing_aspects` へ変換され、本文・citationを保持した
  `insufficient` resultになる。
- `answer: string`、citation marker、live draft、REST/DB/frontend shapeが変わらない。
- prompt、schema、finalizer、orchestrator、live deltaのtestsがgreenになる。
- 定義したモデル受け入れケースで、source単位の列挙ではなく要望・テーマ単位の回答になることを
  手動probeで確認する。probeを実行できない環境では未実行理由を記録する。

## 用語

### Answer composition

質問、content requirements、response requirements、prior coverage、active goalとevidenceを読み、
ユーザーに表示する回答の順序・節・深さ・重点へ変換すること。検索計画や表示stylingは含まない。

### Content requirement

対象、観点、比較軸、期間等、回答内容として満たす単位。独立した要望が複数ある場合、原則として
それぞれに対応する回答箇所を持つ。

### Response requirement

表形式、簡潔さ、深さ、対象読者等、回答全体へ適用する表現条件。response requirementごとの節は
作らない。

### Unfulfilled requirement

回答生成後にも満たせなかった入力 requirement。`c1...` / `p1...` の入力IDで表し、本文の見出しや
使用しなかったevidenceを意味しない。

### Broad research question

「最新動向」「全体像」「業界の状況」「主要な変化」等、複数の事実を統合しなければ直接答えに
ならない質問。単一のcontent requirementでも複数テーマへ整理できる。

## 全体フロー

```text
QuestionContext
  standalone_question
  content_requirements      c1..cN
  response_requirements     p1..pN
  relevant_prior_coverage
  active_goal
        +
normalized evidence         [[source_ref]]
        |
        v
Evidence answer generator
  - 質問全体への直接回答を作る
  - content requirementsを回答箇所へ割り当てる
  - response requirementsを全体へ適用する
  - 広い質問ではevidenceを重要テーマへ束ねる
  - 主張へcitation markerを付ける
  - 満たせなかった入力IDだけを申告する
        |
        v
RawEvidenceAnswerDraft
  sufficiency
  answer
  cited_refs
  missing_aspects
  unfulfilled_requirement_ids
        |
        v
deterministic finalization
  - citation整合
  - 未達IDのclean / allowlist / canonical order
        |
        v
orchestrator assembly
  - 未達IDをdescriptionへ解決
  - final missing_aspectsへ追加
  - 未達ありならinsufficientへcap
  - answer / cited sourcesは保持
```

## 設計判断

### 1. QuestionContextではなくanswererが回答構成を所有する

`QuestionContext` はユーザーが何を求めているかの正本であり、Vector標準の章立てをユーザー要望として
混入させない。例えば「量子コンピュータの最新動向をリサーチして」から、ユーザーが明示していない
「主要企業別」「日本動向」「今後5年」をresponse requirementとして生成しない。

標準構成はevidence answererの編集方針として持つ。

```text
QuestionContext
  -> ユーザーが求めた対象・形式・目的

Evidence answer composition policy
  -> 明示形式がない場合に、根拠を読みやすい回答へ編集する標準方針
```

これにより、ユーザー要望と製品の標準回答品質を分離する。

### 2. content requirementsだけを回答単位として扱う

content requirementsが複数ある場合:

- 原則として入力順に、それぞれを明確に扱う。
- 独立した要望は短い自然な見出しを持つ回答箇所へ分ける。
- 強く関連し、分けるとかえって重複する要望は同じ節で扱ってよい。
- 同じ節で扱っても、全IDの内容へ答えていなければならない。
- `c1` 等のIDを本文へ表示しない。
- requirement descriptionをそのまま見出しへコピーする義務はない。

response requirementsは回答全体へ適用する。

- 「初心者向け」は全節の語彙と説明量へ適用する。
- 「簡潔に」は全体の長さと詳細量へ適用する。
- 「1段落で」があれば、複数content requirementでも見出しを作らない。
- 「表形式で」があれば、標準の文章構成より表形式を優先する。
- `p1` 等のIDを本文へ表示しない。

### 3. 広い質問には適応的な標準構成を使う

明示的なresponse requirementが構成を指定しない場合、answererは質問の広さに応じて構成を選ぶ。

狭い事実質問:

- 冒頭から直接答える。
- 不要な見出しや定型の「概要」「まとめ」を作らない。

広い調査・最新動向・比較質問:

1. 冒頭1〜3文で、質問全体への概要、結論、または現在地を示す。
2. content requirementsだけでは構成が定まらない場合、evidenceからユーザーに重要なテーマを
   原則2〜5件抽出する。
3. テーマを短い自然な見出しの独立行として置き、前後を空行で区切る。
4. 各節は原則「要点、根拠、ユーザーにとっての意味」の順で書く。
5. evidenceが支える場合だけ、最後に現在地、不確実性、今後の注目点をまとめる。

2〜5件はevidence由来テーマの標準値であり、明示された独立content requirementsをまとめるための
上限ではない。content requirementsがそれ自体で必要な回答単位を定める場合は、要望の充足を優先する。

「技術」「企業」「政策」「日本」等の固定taxonomyは作らない。利用するテーマは質問、requirements、
active goal、実際に取得できたevidenceから決める。

### 4. evidenceは主張を支え、入力順を回答順にしない

promptへ次を明示する。

- sourceごとに「Aでは」「Bでは」と順番に紹介しない。
- 複数evidenceが同じ変化を支える場合、共通する傾向として統合する。
- 個別企業・研究成果は、ユーザーの問いを説明するために必要な場合だけ取り上げる。
- 事実、複数根拠から導く傾向、将来見通しを区別する。
- 推論や見通しは、その旨が分かる表現にする。
- 根拠がないテーマを見栄えのために作らない。
- 見出しは事実主張を含まない中立的な短いラベルにし、事実主張は本文へ置く。
- citation markerは見出しではなく、それが支える本文中の主張の直後に置く。
- relevant prior coverageと同じ説明は、今回の理解に必要な場合を除いて繰り返さない。
- active goalは重要度と示唆の方向付けに使い、事実根拠にはしない。

本sliceではevidenceにcollection goalを再付与しない。answererはquestion contextとevidence本文から
テーマを組み立てる。モデル評価で不十分と確認された場合だけ、task correspondenceを保つ後続契約を
別sliceで検討する。

### 5. schemaへ未達IDを1fieldだけ追加する

Gemini response schemaへrequired fieldを追加する。

```json
{
  "sufficiency": "answered",
  "answer": "string",
  "cited_refs": ["1", "2"],
  "missing_aspects": [],
  "unfulfilled_requirement_ids": []
}
```

raw / strict draft:

```python
class RawEvidenceAnswerDraft(BaseModel):
    sufficiency: object | None = None
    answer: object | None = None
    cited_refs: list[object] = Field(default_factory=list)
    missing_aspects: list[object] = Field(default_factory=list)
    unfulfilled_requirement_ids: list[object] = Field(default_factory=list)

class EvidenceAnswerDraft(BaseModel):
    sufficiency: EvidenceAnswerSufficiency
    answer: NonBlankText
    cited_refs: list[str] = Field(default_factory=list)
    missing_aspects: list[NonBlankText] = Field(default_factory=list)
    unfulfilled_requirement_ids: list[str] = Field(default_factory=list)
```

`fulfilled_requirement_ids` は追加しない。入力ID集合から未達IDを引けば、自己申告上の充足IDを
決定的に求められるためである。

`unfulfilled_requirement_ids` は見出し、未使用evidence、追加検索queryではない。今回の回答で
満たせなかったユーザー要望だけを表す。

### 6. sufficiencyとrequirement未達を別の次元として扱う

`EvidenceAnswerDraft.sufficiency` はevidenceの十分性に関するLLM自己申告として維持する。
`unfulfilled_requirement_ids` は回答要望の充足に関する自己申告とする。

```text
sufficiency=answered + unfulfilled=[]
  -> evidenceも回答要望も満たした候補

sufficiency=insufficient
  -> evidence不足。既存missing_aspects契約を使う

sufficiency=answered + unfulfilled=[p1]
  -> evidenceは足りたが形式要望を満たせなかった
  -> final resultはinsufficientへcap
```

strict draftでは `answered + non-empty unfulfilled_requirement_ids` を構築可能とする。
`answered + non-empty missing_aspects` は引き続き禁止する。最終 `AnswerQuestionResult.status` が
evidence不足、retrieval不足、requirement未達を統合する。

#### Synthesis-stage telemetry boundary

`AnswerSynthesisFinalEvent` と `vector.agent.answer_synthesis.outcome` は、finalization済みdraftを表す
synthesis-stage telemetryである。event名のfinalは `EvidenceAnswerFlow` 内のsynthesis完了を意味し、
最終 `AnswerQuestionResult` のstatusやmissing countを表さない。

synthesized resultのtelemetry statusは、`draft.sufficiency` が `insufficient`、またはcanonicalな
`unfulfilled_requirement_ids` が非空なら `insufficient`、それ以外は `answered` とする。
eventの `missing_aspect_count` はsynthesis-stage gap signal数として、次の合計を記録する。

```text
len(draft.missing_aspects) + len(draft.unfulfilled_requirement_ids)
```

このcountにはretrieval empty、collection failure、external task missingを含めず、orchestrationの
description単位のdedupも再現しない。最終user-facing `AnswerQuestionResult.status` とfinal missingの
dedupは引き続きorchestratorだけが所有する。本sliceではevent / metric fieldやfinal-result metricを
追加しない。

### 7. finalizerが未達IDを入力allowlistへ制限する

`EvidenceAnswerFlow` はrequest contextから次のcanonical ID順を作る。

```text
content requirement IDsの入力順
  + response requirement IDsの入力順
```

`finalize_evidence_answer_draft()` へevidenceと別引数で渡す。

```python
finalize_evidence_answer_draft(
    raw,
    evidence=evidence,
    requirement_ids=requirement_ids,
)
```

finalizerは次を決定的に行う。

1. non-stringを除去する。
2. strip後の空文字を除去する。
3. 重複を除去する。
4. 入力に存在しないIDを除去する。
5. content、responseの入力順へ並べ直す。
6. strict draftへ検証済みIDだけを渡す。

補正可能defect code:

```text
unfulfilled_requirement_ids_completed
blank_unfulfilled_requirement_ids_removed
duplicate_unfulfilled_requirement_ids_removed
non_string_unfulfilled_requirement_ids_removed
unknown_unfulfilled_requirement_ids_removed
```

provider schemaではfieldをrequiredにする。raw境界ではdefault emptyを維持し、field欠落時は
`unfulfilled_requirement_ids_completed` を記録して有効なanswerを捨てない。fieldがarrayでない等、
raw model自体を構築できないresponseは既存のvalidation failureとしてretry / fallbackへ流す。
field欠落の判定には、default値との比較ではなく `RawEvidenceAnswerDraft.model_fields_set` を使う。
明示された空配列と欠落を区別し、正当な空配列をdefectとして記録しない。

通常経路での保証所在は `EvidenceAnswerFlow` のfinalizationとし、`EvidenceAnswerer` Protocolにも
「返却draftの未達IDはrequest contextの部分集合である」と明記する。strict draft型単独では
requestごとのallowlistを知れないため、Pydantic validatorへ動的な入力依存検証を持たせない。

### 8. orchestratorが未達IDを最終不足理由へ変換する

evidence answererはIDだけを返し、人間可読文への変換は入力contextを持つorchestratorが
決定的に行う。

```text
c2: 産業界の動きを説明する
  -> 回答要望を満たせませんでした: 産業界の動きを説明する
```

final missingの順序:

1. retrieval empty定型句。
2. collection failure定型句。
3. external task reportのmissing。
4. evidence answer draftのmissing aspects。
5. requirement未達description。content入力順、続いてresponse入力順。

全体を既存どおり完全一致でdedupする。未達IDが1件以上なら、LLMのsufficiencyにかかわらず
最終statusを `insufficient` にする。回答本文、citation marker、解決済みsourcesは保持する。

`_answer_with_evidence()` はrequest contextと検証済みIDから人間可読不足を作り、assemblyへ渡す。

```python
requirement_missing_aspects = _unfulfilled_requirement_missing_aspects(
    context=request.context,
    requirement_ids=draft.unfulfilled_requirement_ids,
)

_assemble_evidence_result(
    ...,
    draft_missing_aspects=draft.missing_aspects,
    requirement_missing_aspects=requirement_missing_aspects,
)
```

assemblyはIDやQuestionContext全体を受けず、最終表示候補の文字列だけを既存missing列の末尾へ加える。

orchestratorはcitation backstopと同様に、draftの未達IDがrequest contextに存在することを再確認する。
strict draftを直接返す別実装・test fakeが通常finalizerを迂回して未知IDを返した場合は、
`EvidenceAnswerDraftInvalidError` をraiseし、入力にない要望をユーザー向け不足へ変換しない。

### 9. answer stringとライブ配信契約を維持する

本文を`sections[]`にしない。Gemini JSON rootの `answer` stringを維持するため、既存
`IncrementalJsonAnswerExtractor` はそのまま利用できる。

追加fieldが `answer` の前後どちらに現れても、extractorはroot objectの他fieldをskipできなければ
ならない。既存の順不同field testへ `unfulfilled_requirement_ids` を含むarray fieldを追加し、
answer deltaがJSON envelopeやassessmentをユーザーへ漏らさないことを確認する。

promptは見出しを短い自然文の独立行と空行で表現させる。Markdown記法やfrontend rendererを
完成条件にしない。

### 10. prompt contract

現行のRole / Task / Citation Rules / Output / untrusted boundaryは維持し、TaskとHard Rulesの間に
次の意味を持つ節を追加する。語尾の調整は実装時に許容するが、規則内容を削らない。

```text
# Primary Objective

最優先の目的は、evidenceを紹介・列挙することではなく、QuestionContextに記録された
今回のユーザー要望へ直接答えることである。

standalone_questionへの回答を中心に置き、content_requirementsを回答内容のチェックリスト、
response_requirementsを回答全体の表現制約として扱う。

# Requirement Handling

- 各content requirementについて、回答本文のどこで扱うかを決める。
- 独立したcontent requirementsが複数ある場合は、原則として入力順に短い自然な見出しを付け、
  それぞれに答える。
- 内容が強く関連するrequirementsは同じ節で扱ってよいが、いずれも明確に回答する。
- requirement IDをユーザー向け本文に表示しない。
- response requirementsは回答全体へ適用し、response requirementごとの節は作らない。
- response requirementが別の構成を指定する場合は、標準の章立てより明示要望を優先する。

# Default Answer Composition

- 冒頭1〜3文で、質問全体への結論、概要、または現在地を直接示す。
- 狭い事実質問では、不要な見出しを作らず簡潔に答える。
- 最新動向、業界調査、比較、全体像等の広い質問では、明示content requirementが1件でも、
  content requirementsだけでは構成が定まらない場合に、evidenceから重要なテーマを原則2〜5件
  抽出して整理する。
- 2〜5件はevidence由来テーマの標準値であり、独立content requirementsを落とす上限にしない。
- テーマはevidenceの並び順ではなく、質問とactive_goalに対する重要度で並べる。
- 各節は原則として「要点、根拠、ユーザーにとっての意味」の順で書く。
- 複数evidenceが同じ傾向を示す場合、個別ニュースを並べず共通する動向として統合する。
- 根拠がないテーマや、見栄えを整えるためだけの節は作らない。
- relevant_prior_coverageと同じ説明は、今回必要な場合を除いて繰り返さない。

# Evidence Use

- evidenceは回答を支える根拠であり、回答構成そのものではない。
- source単位の順番で事実を列挙しない。
- evidenceから確認できる事実、複数根拠から導ける傾向、将来の見通しを区別する。
- 推論や見通しは、その旨が分かる表現にする。
- 見出しは事実主張を含まない中立的な短いラベルにする。
- citation markerは、それが支える本文中の主張の直後に置き、見出しには付けない。

# Completion Assessment

- 出力前に全content/response requirementを満たしたか確認する。
- 十分なevidenceがないcontent requirementを黙って省略しない。本文で不足を明示し、
  そのIDをunfulfilled_requirement_idsへ入れる。
- 対象漏れ、比較軸漏れ、明示形式の不履行も未達として扱う。
- 満たせなかった入力requirementのIDだけを返し、入力にないIDを作らない。
- 全requirementsを満たした場合、unfulfilled_requirement_idsは空配列にする。
- 確認過程や内部チェックリストは回答本文へ出力しない。
```

既存promptの次も維持する。

- contextは回答の形を決めるが事実根拠ではない。
- evidenceにない事実を確認済み事実として扱わない。
- no evidence時の断りとcitation禁止。
- inline citation marker、cited refs、missing aspectsの既存契約。
- References / Sourcesセクションを作らない。

### 11. schema descriptionは制約を短く重ねる

詳細な回答編集規則のSSoTはpromptとする。Gemini schemaのdescriptionには、fieldの意味と
機械的制約だけを記す。

`answer` description:

```text
Japanese answer shown to the user. It must directly answer the question, follow the
provided requirements, and keep inline citation markers after supported claims.
```

`unfulfilled_requirement_ids` description:

```text
IDs of provided content or response requirements that the generated answer did not
fulfill. Use only IDs present in the prompt, preserve input order, and return an empty
array when all requirements were fulfilled.
```

promptとschemaへ長い同文を二重に置かず、schemaだけを読んでもIDの意味を誤解しない状態にする。

### 12. 自己評価の限界を受け入れ、最初からcheckerを追加しない

同じgeneration callが本文と未達IDを返すため、見落としや過剰申告は起こり得る。本sliceでは
独立checkerを追加しない。

優先順位:

```text
1. citation / schema / allowlist等の決定的validator
2. ユーザーが次turnで示す明示的feedback
3. answererのunfulfilled requirement自己申告
```

出力品質はモデル受け入れケースで確認し、未達の自己申告と後続ユーザーfeedbackの乖離が大きい場合に
だけcheckerまたは形式別validatorを後続検討する。

## Failure behavior

| Failure / defect | Behavior |
|---|---|
| `unfulfilled_requirement_ids` 欠落 | 空配列へ補完しdefect記録、answerを保持 |
| fieldがarrayでなくraw draftを構築不能 | 既存response validation failureとしてretry / fallback |
| non-string / blank ID | 除外しdefect記録 |
| 重複ID | 初出だけ保持しdefect記録 |
| 入力にないID | 除外しdefect記録 |
| strict draftがfinalizerを迂回して未知IDを返す | orchestrator backstopでtyped error、表示用不足へ変換しない |
| 既知の未達IDが1件以上 | descriptionへ変換しfinal missingへ追加、statusをinsufficientへcap |
| answer本文がschema validだが構成品質が低い | 決定的repairはせず、model eval / user feedbackで観測 |
| citation marker不整合 | 既存の補正またはretry / fallback契約を維持 |
| provider error / JSON不正 | 既存EvidenceAnswerFlowのretry / fallback契約を維持 |
| defect/audit送信失敗 | best-effort、runへ伝播しない |

## Security / prompt safety

- question、requirement description、coverage、goal、evidenceは全てuntrusted textである。
- requirement IDだけを制御値として扱い、description内の命令をsystem instructionとして実行しない。
- 既存 `sanitize_for_untrusted_block()` 境界を維持する。
- `unfulfilled_requirement_ids` は入力ID allowlistで検証し、自由文を受け付けない。
- requirementやactive goalを引用根拠にしない。
- answer構成規則を理由に、evidenceにない一般論や固定テーマを補わない。
- question、requirement、answer、evidence本文をlog・metric labelへ載せない。

## API / DB / dependency

- REST request / response shapeは変更しない。
- `ResearchAssistantMessage`、`AnswerQuestionResult`へ新fieldを追加しない。
- 未達IDはrun内部だけで使い、既存 `missing_aspects` へdescriptionとして畳む。
- DB schema、SQLModel model、migrationを変更しない。
- frontend generated typesを変更しないため `/gen-types` は不要。
- 新規dependencyを追加しない。
- authn / authz、thread ownership、run ownershipを変更しない。

## 想定変更ファイル

```text
backend/app/agent/answering/evidence_answer/ai/prompt.py
  - answer composition / requirement assessment規則

backend/app/agent/answering/evidence_answer/ai/schema_tool.py
  - unfulfilled_requirement_ids required field
  - answer / assessment description

backend/app/agent/answering/evidence_answer/contract.py
  - raw / strict draft field

backend/app/agent/answering/evidence_answer/validation.py
  - 未達ID clean / allowlist / canonical order / defect codes

backend/app/agent/answering/evidence_answer/flow.py
  - request contextからcanonical requirement IDsをfinalizerへ渡す
  - canonical未達IDをsynthesized event / metricのstatusとgap countへ反映する

backend/app/agent/answering/orchestration.py
  - 未達IDからdescriptionへの変換
  - final missingへの追加とinsufficient cap

backend/tests/agent/answering/evidence_answer/ai/test_prompt_schema.py
backend/tests/agent/answering/evidence_answer/test_contract.py
backend/tests/agent/answering/evidence_answer/test_validation.py
backend/tests/agent/answering/evidence_answer/test_flow.py
backend/tests/agent/answering/test_orchestration.py
backend/tests/agent/live_updates/test_answer_delta_integration.py
```

`audit.py` は既存の汎用defect eventでcodeを記録できるため、field追加は行わない。
専用event / metric fieldは追加せず、既存のsynthesis outcome / defect telemetryを維持する。
最終 `AnswerQuestionResult` 専用metricは本sliceで追加しない。

## Tests

### Prompt / schema

1. promptにPrimary Objective、Requirement Handling、Default Answer Composition、Evidence Use、
   Completion Assessmentが含まれる。
2. 複数content requirementsを個別に扱い、response requirementsを全体へ適用する規則がある。
3. 広い質問では2〜5テーマ、狭い質問では不要な見出しを作らない規則がある。
4. evidence順の列挙禁止、事実・傾向・見通しの区別、citation配置規則がある。
5. requirement IDを本文へ表示せず、入力IDだけを未達申告する規則がある。
6. schema required fieldsに `unfulfilled_requirement_ids` が含まれる。
7. assessment fieldがstring arrayで、入力IDのみ・未達なしは空配列というdescriptionを持つ。
8. schema / prompt変更によりcall signature versionが変わる。
9. question、requirements、coverage、goal、evidenceのuntrusted boundary sanitizationを維持する。

### Contract / finalization

10. strict draftが空・非空のunfulfilled IDsを保持する。
11. answered + non-empty unfulfilled IDsはstrict draftとして許可するが、answered + missingは拒否する。
12. validな入力IDはcontent、responseのcanonical orderで残る。
13. blank、non-string、duplicate、unknown IDを除去し、それぞれdefectを記録する。
14. field欠落は空配列へ補完され、completion defectを記録する。
15. fieldがarrayでないresponseはraw validation failureになる。
16. requirements空の場合、申告IDは全てunknownとして除外される。
17. citation marker / cited refs / missing aspectsの既存補正を壊さない。

### Synthesis telemetry

- answered draftかつ未達IDなしでは、synthesized event / metricのstatusを `answered` のまま保つ。
- answered draftかつcanonical未達IDが2件では、draftを変更せずtelemetry statusを `insufficient`、
  `missing_aspect_count` を2とする。
- insufficient draftでmissing aspectが1件、canonical未達IDが2件では、telemetry statusを
  `insufficient`、`missing_aspect_count` を3とする。

### Orchestration

18. 未達なしのvalid draftは従来どおりanswered resultになる。
19. evidence sufficiencyはansweredでも、content未達IDがあれば本文・sourcesを保持したinsufficientになる。
20. response未達IDも同じくinsufficientになる。
21. 未達IDを入力descriptionへ解決し、決めたprefixでmissing_aspectsへ追加する。
22. requirement missingは既存不足理由の後、content、response入力順で並ぶ。
23. 既存不足理由との完全一致dedupを維持する。
24. unknown IDはorchestratorへ届かない。
25. finalizerを迂回したstrict draftのunknown IDはorchestrator backstopで拒否される。

### Live answer delivery

26. assessment arrayがanswerより前にあるJSONでも、answer文字列だけをdelta配信する。
27. assessment arrayがanswerより後にあるJSONでも、answer文字列だけをdelta配信する。
28. JSON envelope、requirement ID、assessment fieldをユーザー向けdeltaへ漏らさない。
29. retry / reset / fallbackの既存generation契約を維持する。

## Model acceptance cases

unit testはpromptと決定的contractを保証するが、文章品質そのものは保証しない。実Geminiを使う
`backend/scripts/probe_question_answering.py` または同等の手動probeで次を確認する。

| Case | Context / evidence | Acceptance |
|---|---|---|
| 広い最新動向 | c1=量子コンピュータの最新動向、技術・企業・政策の複数evidence | 冒頭概要 + 2〜5テーマ。source順の一段落列挙にならない |
| 複数要望 | c1=技術進歩、c2=産業動向、c3=現在地 | 各要望に明確な回答箇所があり、IDは本文に出ない |
| 明示形式優先 | c1/c2複数、p1=1段落で簡潔に | 標準見出しより1段落要求を優先し、両内容を扱う |
| 根拠不足 | c1は十分、c2を支えるevidenceなし | c1本文を保持し、c2不足を本文で明示、unfulfilled=[c2] |
| 狭い質問 | 単一の具体的事実と十分なevidence | 不要な概要・章立てを作らず直接回答する |
| prior coverage | 関連説明済み内容あり | 必要な接続を除き既出説明を繰り返さない |
| active goal | 投資判断等の明示目的あり | 重要度・意味を目的へ寄せるが、goalを事実根拠にしない |

1回の出力だけで品質を断定せず、少なくとも広い質問と複数要望を複数回確認する。モデル出力は
固定golden textとの完全一致ではなく、上表の構成・充足・citation条件で評価する。

## 実装順序

1. prompt / schema testsをredにする。
2. evidence draft contractとfinalizerの未達ID契約を実装する。
3. Gemini schemaとprompt composition規則を実装する。
4. flowからcanonical requirement IDsをfinalizerへ配布する。
5. orchestratorでdescription解決、missing追加、status capを実装する。
6. live delta regression testsを通す。
7. backendの対象unit / integration testsを実行する。
8. `/check` を実行する。
9. 可能ならmodel acceptance probeを実行し、結果と未実行項目を記録する。

## Verification

最低限、次を実行する。

```bash
cd backend
uv run pytest tests/agent/answering/evidence_answer -q
uv run pytest tests/agent/answering/test_orchestration.py -q
uv run pytest tests/agent/live_updates/test_answer_delta_integration.py -q
```

その後、repositoryの `/check` skillに従いlint、format、types、unit、必要なintegrationを確認する。
