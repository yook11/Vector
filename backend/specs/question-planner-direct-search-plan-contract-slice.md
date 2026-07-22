# Question Planner Direct / Search Plan Contract slice 仕様

更新日: 2026-07-23

Status: Implemented

実装状況: 実装済み

## 位置付け

本sliceは、Question Plannerの4つのrouting planを、回答方法を表す次の2つのplanへ
置き換える。

- `DirectAnswerPlan`: 検索せずに回答する。
- `SearchPlan`: Vectorの分析済み記事検索と外部リサーチを常に両方実行して回答する。

Plannerは「内部だけ」「外部だけ」という実行経路を選ばない。検索が必要かを判断し、
必要な場合は「どの検索文で分析済み記事を探すか」と「外部で何を調べるべきか」を
同時に計画する。実際の外部検索keyword queryは、既存どおり下流のExternal Query Agentが
research goalから生成する。

`target_time_window`は改名しない。この値は質問が対象とする出来事の時期ではなく、
外部根拠として収集する情報の公開・更新期間を表す。`publication_window`よりも、Plannerが
選んだ外部検索対象期間であることを読み取りやすいため、既存名を維持する。内部記事の
公開期間を拘束する値ではない。

### 先行仕様との関係

本sliceは、次の先行契約のうちQuestion Plannerのplan数、名前、fallback、
dispatchに関する条項を置き換える。各仕様が定めるAgent Runtime、外部research task、
並行実行、期間解決の責任境界は、本仕様と矛盾しない範囲で継承する。

- `planner-agent-runtime-slice.md`
- `question-answering-external-research-task-contract-slice.md`
- `external-query-selector-agent-runtime-slice.md`
- `retrieval-dispatch-ownership-slice.md`
- `external-search-tavily-date-filter-slice.md`
- `question-answering-answer-orchestration-slice.md`

実装と検証に合わせて`specs/agent`全体を検索し、旧4値・旧field・fallbackを
現行契約として記述していた次の公開仕様を更新または統合した。公開仕様に旧契約の墓標は残さない。

- `specs/agent/question-planner-routing.md`
- `specs/agent/question-plan-variant-types.md`
- `specs/agent/internal-query-cap-and-planner-draft-audit.md`
- `specs/agent/question-planner-audit.md`
- `specs/agent/internal-retrieval-query-embedding.md`
- `specs/agent/internal-retrieval-article-search.md`

### 仕様の固定レベル

本仕様が固定するのは、Plannerの出力契約、名前、retry後の停止条件、検索経路、
失敗意味論、外から観測できる結果である。同じ契約を満たすprivate helper名、validatorの
分割、並行実行primitive、test double名は固定しない。

## Work Definition

### Problem

1. 現行の`none` / `internal` / `external` / `internal_and_external`は、Plannerへ
   検索要否だけでなく実装上の検索経路まで選ばせている。
2. 4つのmodeに対応する4つのplan型、Runner分岐、結果判定があり、検索が必要という
   1つの判断に対して状態数が多い。
3. `none`は否定形、`internal_queries`は検索対象が不明瞭、
   `external_collection_goals`はPlannerが外部経路を選ぶように見え、役割を名前から
   読み取りにくい。
4. 現行Plannerは有効なplanを作れないと、質問文をqueryへ流用したinternal-only planへ
   silent fallbackする。Plannerの判断が失われた状態で処理が継続し、ユーザーが要求した
   外部根拠も収集されない。
5. `retrieval_mode`はPlannerだけでなく、完成結果の内部summary、Runner分岐、metric labelへ
   波及しており、「情報取得経路」と「回答plan」の意味が混在している。
6. external-only planに固有の結果組み立て特例があり、同じ外部期間失敗でもmixed planと
   missingの意味が異なる。

### Evidence

- `app/agent/planning/contract.py`
  - `QuestionPlanDraft`が4値の`retrieval_mode`、`internal_queries`、
    `external_collection_goals`、`target_time_window`、`reason`を持つ。
  - `NoRetrievalPlan`と3つのretrieval planがあり、`plan_from_draft()`がmodeごとに分岐する。
  - queryまたはgoalが空になると`standalone_question`で補い、Planner失敗時は
    `safe_fallback_plan()`がinternal-only planを返す。
- `app/agent/planning/prompts.py` / `planning/ai/schema_tool.py`
  - modelへ4 modeの選択、mode別の配列制約、routing reasonを要求している。
  - model-visibleなprompt/schemaの手動revisionは現在`v2`である。
- `app/agent/planning/service.py`
  - response defectだけを最大2 attemptまでrepair retryし、回復しない場合や分類済み
    provider failureではfallback planを返す。
- `app/agent/running/answering_runner.py`
  - 4 planをdirect / internal / external / mixedへdispatchし、mixedだけが内部検索と
    外部リサーチを並行実行する。
- `app/agent/evidence_collection/internal_search`
  - Plannerの内部queryはembeddingされ、pgvectorによる分析済み記事検索へ使われる。
- `app/agent/evidence_collection/external_search`
  - `ExternalQueryGenerationInput`はresearch task、`as_of`、`target_time_window`を受け、
    External Query Agentがgoalを外部ニュース検索用の英語keyword query 1〜3件へ変換する。
- `app/agent/answering/result_assembly.py`
  - external-only planで全taskが期間解決に失敗した場合だけ、根拠0件の一般missingを
    抑制する特例がある。
- `app/agent/contract.py` / `planning/metrics.py`
  - 4値の`RetrievalMode`が`AnswerRetrievalSummary.planned_mode`と
    `planned_retrieval_mode` metric attributeへ使われる。
- `app/agent/runs/result_mapper.py`、`app/schemas/research.py`、frontend generated types
  - plan typeとretrieval summaryはassistant messageへ永続化されず、公開APIやfrontendへ
    投影されていない。

### Invariants

- 完成済み`QuestionPlan`は`DirectAnswerPlan | SearchPlan`の2 variantだけとする。
- Plannerが選ぶ値は`direct_answer | search`だけとし、内部・外部・mixedというrouting
  判断をPlanner contractへ残さない。
- validな`SearchPlan`は、分析済み記事検索queryと外部research goalをそれぞれ1〜3件
  必ず持つ。どちらか一方だけの検索planは表現できない。
- `SearchPlan`を受けたRunnerは内部記事検索と外部リサーチを常に両方開始し、固定2枝として
  並行実行する。fieldの有無による隠れたdispatchを追加しない。
- Plannerは外部検索keyword queryを生成しない。Plannerはresearch goalを決め、External Query
  Agentがgoal、`as_of`、`target_time_window`から実行queryを生成する。
- Plannerの最終失敗時はplanを捏造せず、回答runを停止する。internal-only、external-only、
  SearchPlanのいずれにもfallbackしない。
- Plannerのresponse defectに対する現在の最大2 attemptは維持する。retry不能な失敗または
  最終attemptの失敗後に検索や回答生成を開始しない。
- `target_time_window`と`TargetTimeWindow`の名前・閉じたkind・JST基準の解決規則を維持する。
- `target_time_window`の厳密な日付filterは外部検索へだけ適用する。内部記事検索へ期間filterを
  追加しない。
- mixed dispatchで実装済みの両側完走、internal例外優先、成功側side effect維持、
  cancellation時に両枝をcancelして終了を待つこと、external resource closeの契約を、唯一の
  SearchPlan経路へ継承する。
- user input、previous error、model output、provider exceptionの安全化境界を弱めず、metric、span、
  logへ自由文や生responseを追加しない。
- HTTP API、DB、Redis event、Taskiq message、frontend typeのshapeを変更しない。

### Non-goals

- 内部記事検索へ`target_time_window`の厳密な日付filterを追加すること。
- External Query Agent、External Evidence Selector、Tavily providerの検索・選別policyを変更すること。
- research goalをPlanner内で外部keyword queryへ変換すること。
- Direct Answer AgentまたはEvidence Answer Agentのmodel、prompt、retry policyを変更すること。
- Plannerのmodel、temperature、token上限、providerを変更すること。
- 新しいfallback、hidden mode、設定flag、dependencyを追加すること。
- 新しい公開エラーcode、UI表示、progress stage、activity eventを追加すること。
- API response、永続化schema、認証・認可を変更すること。
- 本仕様の作成だけで、現行実装済み公開仕様やtest guarantee ledgerを先行更新すること。

### Done

- Plannerのmodel-visibleな選択肢と完成済みdomain planが2 variantだけになる。
- `SearchPlan`がqueryとgoalの両方を型で要求し、valid planから内部だけ・外部だけの実行が
  発生しない。
- Plannerの最終失敗が回答runのfailed終了になり、検索・回答・assistant message保存へ進まない。
- 旧mixed pathの並行・失敗・cancellation契約がすべてのSearchPlanで維持される。
- `target_time_window`の名前と意味、外部検索・External Query Agent・Evidence Answer Agentへの
  伝播が維持される。
- Planner、Runner、内部summary、metricから旧4値modeと旧variant名がなくなる。
- 公開API、DB、frontend shapeを変更せず、対象unit / integration testと`/check`が成功する。
- 実装完了時に公開仕様を現行2 plan契約へ更新し、旧4 planを正本とする重複仕様を残さない。

## 決定済みの命名と必要な追随

表のPlanner draft / plan名は本sliceの中心となる決定である。constant、実行task、内部summary、metricは、
同じ概念へ旧4 mode / retrieval / collectionの語彙を残さないために必要なconsumer追随として固定する。

| 現在 | 新しい名前・値 | 意味 |
|---|---|---|
| `RetrievalMode` | `PlanType` | Plannerが選ぶ回答planの判別値 |
| `retrieval_mode` | `plan_type` | plan / draftのdiscriminator |
| `none` | `direct_answer` | 検索を使わない回答 |
| `internal` | `search`へ統合 | 内部記事検索と外部リサーチを両方実行 |
| `external` | `search`へ統合 | 内部記事検索と外部リサーチを両方実行 |
| `internal_and_external` | `search` | 唯一の検索topology |
| `NoRetrievalPlan` | `DirectAnswerPlan` | 直接回答plan |
| `InternalRetrievalPlan` | `SearchPlan`へ統合 | 検索回答plan |
| `ExternalSearchPlan` | `SearchPlan`へ統合 | 検索回答plan |
| `InternalAndExternalPlan` | `SearchPlan` | 検索回答plan |
| `internal_queries` | `article_search_queries` | Vectorの分析済み記事を探すsemantic query |
| `external_collection_goals` | `research_goals` | 外部で確認すべき根拠・観点 |
| `ExternalResearchTask.collection_goal` | `ExternalResearchTask.research_goal` | draftと実行taskで同じresearch goal語彙を使う |
| `MAX_INTERNAL_QUERIES` | `MAX_ARTICLE_SEARCH_QUERIES` | 分析済み記事検索queryの上限 |
| `reason` | 削除 | production consumerがないrouting説明 |
| `safe_fallback_plan` | 削除 | Planner失敗時はrunを停止する |
| `AnswerRetrievalSummary` | `AnswerPlanSummary` | 選択planと収集失敗の一次事実をまとめる内部summary |
| `AnswerQuestionResult.retrieval` | `AnswerQuestionResult.plan_summary` | retrieval以外のdirect planも含むplan summary |
| `planned_mode` | `plan_type` | final result内部のplan識別 |
| metric `planned_retrieval_mode` | metric `plan_type` | Planner成功時の2値label |
| `target_time_window` | 維持 | 外部根拠として対象にする公開・更新期間 |

`article_search_queries`を採用し、単なる`search_queries`は採用しない。外部researchでも実行時に
search queryを生成するため、対象を名前に含めないと2種類のqueryを区別できないためである。

## Domain Contract

### Planner draftと完成済みplan

model向けwire schemaはflatなobjectを維持し、配列fieldを常に要求する。Python側のsemantic
validationと完成済みdiscriminated unionが、`plan_type`とfieldの組み合わせを保証する。

```python
PlanType = Literal["direct_answer", "search"]


class QuestionPlanDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_type: PlanType
    article_search_queries: list[str]
    research_goals: list[str]
    target_time_window: TargetTimeWindow | None = None


class DirectAnswerPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_type: Literal["direct_answer"] = "direct_answer"


class ExternalResearchTask(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    research_goal: str = Field(min_length=1)


class SearchPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_type: Literal["search"] = "search"
    article_search_queries: list[PlanQuery] = Field(
        min_length=1,
        max_length=MAX_ARTICLE_SEARCH_QUERIES,
    )
    external_research_tasks: list[ExternalResearchTask] = Field(
        min_length=1,
        max_length=EXTERNAL_RESEARCH_TASK_LIMIT,
    )
    target_time_window: TargetTimeWindow | None = None

    @model_validator(mode="after")
    def _validate_unique_inputs(self) -> Self:
        query_keys = [query.casefold() for query in self.article_search_queries]
        if len(query_keys) != len(set(query_keys)):
            raise ValueError("article search queries must be unique")
        goals = [task.research_goal for task in self.external_research_tasks]
        if len(goals) != len(set(goals)):
            raise ValueError("research goals must be unique")
        return self


type QuestionPlan = DirectAnswerPlan | SearchPlan
```

`QuestionPlanDraft.research_goals`はPlannerが返す文字列配列であり、完成時に
`ExternalResearchTask(research_goal=...)`へ1対1で変換する。Plannerは回答要件を満たすための
調査観点の分解とgoal数を所有する。外部pipelineはtask数と実行設定からeffective agent数を導出し、
検索query、agent数、並列度、候補数をPlannerへ選ばせない。

`collection_goal`は`research_goal`と同じ概念を別名で表しているため、本sliceでtask fieldも
`research_goal`へ追随する。これは外部pipelineのpolicy変更ではなく、contractとconsumerの機械的な
renameである。

`reason`は完成済みplan、dispatch、metric、answer生成、永続化のいずれにも使われていないため
削除する。Plannerの出力tokenを、実行結果を変えない自己説明へ使わない。

### 正規化とsemantic validation

draftから完成済みplanを作る際は、次をこの順序で適用する。

1. queryとgoalの前後空白を除去し、空白だけの要素を除去する。
2. queryはcase-insensitive、goalはstrip後の文字列一致で重複を除去し、最初の要素を残す。
3. それぞれ先頭3件までに制限する。
4. `plan_type=direct_answer`では、両配列が空かつ`target_time_window=null`でなければ
   inconsistent responseとして扱う。
5. `plan_type=search`では、正規化後のqueryまたはgoalが0件ならinconsistent responseとして扱う。
6. validなgoalを`ExternalResearchTask.research_goal`へ変換し、`SearchPlan`を構築する。

inconsistent responseは質問文による補完やfallbackを行わず、安全なresponse defectとして
repair retryの対象にする。最終attemptでも修復できなければplanを返さない。

完成済み`DirectAnswerPlan`はquery、goal、期間をfieldとして持たない。完成済み`SearchPlan`は
queryとtaskの両方を必須にするため、validatorを迂回しない限り片側だけの検索を表現できない。

## Planner Prompt / Wire Schema

### 判断規則

Planner instructionsは、内部・外部の経路分類ではなく次の2択だけを要求する。

- `direct_answer`: 挨拶、Vectorの使い方、既存回答の言い換え、入力済み文章の変換など、
  新しい事実根拠を収集せずに完結する。
- `search`: ニュース、企業、投資判断、株価、規制、セキュリティ、研究発表、最新性、
  日付相対表現を含む事実質問、または回答要件を満たすために根拠収集が必要である。

迷った場合は`search`とする。`content_requirements`は必要な調査対象・観点・比較軸へ反映し、
`response_requirements`の形式・文体・簡潔さだけを理由に検索を増減させない。conversation contextは
計画の文脈であり、事実根拠として扱わない。

### field規則

- `direct_answer`
  - `article_search_queries=[]`
  - `research_goals=[]`
  - `target_time_window=null`
- `search`
  - `article_search_queries`: 分析済み記事をsemantic searchする自然文を1〜3件。raw questionを
    そのままコピーせず、entity / topic / event / time intentを抽出・圧縮する。
  - `research_goals`: 外部で何を確認し、何が回答根拠として有用かを表す短い日本語を1〜3件。
    keyword queryは書かない。
  - `target_time_window`: 外部根拠の公開・更新期間を意図的に限定する場合だけ型付き値を返し、
    それ以外は`null`とする。内部記事へ同じ期間保証があるように表現しない。

例:

```json
{
  "plan_type": "search",
  "article_search_queries": [
    "NVIDIA AI GPU データセンター 発表 提携 業績 規制 直近動向"
  ],
  "research_goals": [
    "NVIDIAの直近の発表・提携・業績に関する報道を確認する",
    "製品の供給・需要の変化が投資判断に与える影響を確認する"
  ],
  "target_time_window": {"kind": "last_n_days", "days": 7}
}
```

```json
{
  "plan_type": "direct_answer",
  "article_search_queries": [],
  "research_goals": [],
  "target_time_window": null
}
```

manual Gemini response schemaのenum、required field、descriptionを上記へ合わせ、
`reason`を削除する。model-visibleなinstructionsとschemaを変更するため、
`PLANNER_PROMPT_VERSION`は`v2`から`v3`へ明示的に更新する。`QuestionContext`、`as_of`、
`previous_error`のsanitizeと`<untrusted_input>`境界は維持する。

`ExternalResearchTask.collection_goal`を`research_goal`へ変更すると、External Query Agentと
External Evidence Selector Agentのmodel-visibleなinput templateも`collection_goal:`から
`research_goal:`へ変わる。検索・選別instructionsのpolicyは変更しないが、固定input templateの変更は
prompt revision対象であるため、`EXTERNAL_QUERY_PROMPT_VERSION`と
`EXTERNAL_EVIDENCE_SELECTOR_PROMPT_VERSION`をそれぞれ`v1`から`v2`へ更新する。

## Planning Service / Failure Contract

### Retryと停止

1つのplanning phaseでPlanner Runtime scopeを1回だけ開始し、現在と同じ最大2 attemptで同じclientを
共有する。

1. attempt 1がvalidな完成済みplanを作れれば返す。
2. invalid JSON、object以外、schema不一致、または正規化後のquery / goal不足など、安全に分類した
   response defectだけは`previous_error`を渡してattempt 2を実行する。
3. attempt 2でもresponse defectなら、その分類済み例外を伝播してrunを停止する。
4. provider state / content errorなど、現在`DO_NOT_RETRY_IN_REQUEST`である失敗はattempt内retryせず、
   その分類済み例外を伝播してrunを停止する。
5. Runtime scopeの開始・終了失敗、未分類例外、`CancelledError`もfallbackへ変換せず伝播する。

semantic validation defectをrepairへ載せる実装方法は固定しないが、raw model output、Pydantic input、
自由文validation messageを例外graph、prompt、metric、logへ漏らしてはならない。repair inputへ渡せるのは
既存`AgentResponseInvalidError`と同等のlow-cardinalityなdefectと安全なrepair hintだけとする。

`safe_fallback_plan()`と`fallback_query`による補完は削除する。Planner失敗後にQuestion Contextを
External Query Agentへ直接渡す、external-only検索を起動する、質問文からSearchPlanを組み立てる、といった
迂回経路は追加しない。

### Workerへの伝播

- `AIProviderError`または`AgentResponseInvalidError`として分類できる最終失敗は、既存worker境界で
  `generation_unavailable`へ写像し、runをfailed terminalへ遷移させる。
- 未分類例外は既存どおり`internal_error`へ写像する。
- `CancelledError`はfailure resultやfallback planへ変換しない。
- Planner失敗後は`retrieving` / `synthesizing`へ進まず、assistant messageとsourceを保存しない。
- 新しい公開error codeや例外messageは追加しない。

### Observability

`vector.agent.planner.outcome`は維持し、次へ更新する。

- `result`: `planned | failed`。`fallback`を削除する。
- `retry_used`: attempt 2を開始したかを表す。
- `plan_type`: 成功時は`direct_answer | search`、完成planがない失敗時は`not_created`。
  `not_created`はmetric専用sentinelであり、`PlanType`へ追加する第3のplanではない。
- `failure_code`: 分類済み失敗code。成功時は`none`。

成功またはPlanner境界で分類済みの最終失敗ごとにexactly once記録する。`planned` / `failed`の
どちらもRuntime scopeの退出処理が別の例外を出さず完了した後に記録する。`planned`はplanを呼び出し元へ
返せる場合、`failed`は分類済みattempt失敗を呼び出し元へ伝播する場合に限る。scopeの開始・終了失敗、
未分類例外、`CancelledError`ではoutcome metricを記録せず、既存phase spanのerror / cancellation
意味論を維持する。attributeへquestion、query、goal、期間内容、raw response、例外messageを記録しない。

## Answering Runner Contract

### Dispatch

```text
QuestionPlan
  DirectAnswerPlan
    -> Direct Answer Agent

  SearchPlan
    -> progress: retrieving
    -> 内部記事検索と外部リサーチを同時に開始
         article_search_queries -> internal analyzed-article search ─┐
         external_research_tasks -> external research pipeline      ─┘ 両方の終了を待つ
    -> Evidence Answer Agent
```

- `DirectAnswerPlan`では内部検索、external runtime activation、Evidence Answer Agentを0回とする。
- `SearchPlan`では内部枝と外部枝を一方の完了待ちなしに開始する。内部だけ・外部だけのbranchを
  残さない。
- `retrieving`は両枝の起動前に1回、`synthesizing`は両方の処理終了とexternal resource close後に
  1回通知する。
- internal / external activity eventは並行実行によりinterleaveし得るため、枝をまたぐ厳密な順序を
  契約にしない。各枝内の既存順序は維持する。

### 並行失敗意味論

現行`InternalAndExternalPlan`経路の契約を、すべての`SearchPlan`へ適用する。

- `InternalSearchError`は`collection_failures=["internal_search"]`へ変換し、外部outcomeを保持する。
- external task / queryの分類済みfailureはtask reportに保持し、内部evidenceを失わない。
- 片枝で未分類例外が起きても他枝の終了を待ち、成功側のcache、metric、event、task reportを維持する。
- 両枝が未分類例外ならinternal例外を優先する。internalが成功または分類済みfailureならexternal例外を
  同じinstanceのまま伝播する。
- outer cancellationでは開始済みの両枝とexternal pipeline内の子処理をcancelして終了を待ち、
  external resourceをcloseした後に元の`CancelledError`を伝播する。

### `target_time_window`の伝播

`SearchPlan.target_time_window`は外部根拠収集の期間制約として、次へ同じtyped値または解決結果を渡す。

1. External Query Agent input。
2. JST基準のexternal date filter resolver。
3. 各External Search Tool / Tavily request。
4. Evidence Answer Agent prompt。外部検索へ適用した期間制約とcoverageを解釈する文脈であり、
   内部記事が同じ期間内にあるという保証には使わない。

`article_search_queries`には質問のtime intentを表現できるが、内部DBのstrict date filterは追加しない。
Evidence Answer Agentも、`target_time_window`だけを理由に内部evidenceを期間内とみなしたり、期間外として
機械的に除外したりしない。
`unsupported_explicit_window`または解決不能期間ではexternal runtimeを起動せず、全external taskを
`time_filter_failed`として報告する。同時に開始した内部枝はcancelせず完了させる。

## Result Contract

`AnswerRetrievalSummary`はdirect planも収納しており名前と責務が一致しないため、agent core内部の
`AnswerPlanSummary`へ変更する。`AnswerQuestionResult.retrieval`も`plan_summary`へ追随する。

```python
class AnswerPlanSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_type: PlanType
    collection_failures: list[EvidenceCollectionFailure] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_direct_answer_has_no_collection_failure(self) -> Self:
        if self.plan_type == "direct_answer" and self.collection_failures:
            raise ValueError("direct answer cannot have collection failures")
        return self
```

`AnswerPlanSummary`が持つのはPlannerが選んだ`plan_type`と分類済みcollection failureという一次事実だけで
ある。削除済みの`AnswerExecutionSummary` / `ExecutionRoute` / `used_*`のように、sourceから導出できる
実行経路labelを再導入しない。

- `plan_type=direct_answer`のanswered resultはsourceを持たない。
- `plan_type=search`のanswered resultは内部記事または外部URLのsourceを1件以上持つ。
- collection failureまたはmissingがあれば既存どおり`insufficient`とする。
- `direct_answer`とcollection failureの組み合わせは完成型で拒否する。
- plan summaryはassistant message、DB、HTTP response、frontendへ新たに投影しない。

`ExternalSearchPlan`がなくなるため、external-onlyで全taskが`time_filter_failed`のときだけ
一般的な根拠0件missingを抑制する特例は削除する。`SearchPlan`は旧mixed semanticsへ統一する。

| 状態 | 結果 |
|---|---|
| external全taskが期間失敗、internal evidenceあり | internal evidenceを保持し、期間失敗、draft missing、未達要件を反映 |
| external全taskが期間失敗、internal evidenceなし | 根拠0件、期間失敗、未達要件を反映し、一般的なdraft missingは抑制 |
| `InternalSearchError`、external evidenceあり | external evidenceを保持し、internal collection failureをmissingへ反映 |
| external taskがquery / provider / selectorで分類済みfailure、internal evidenceあり | task reportを保持し、既存`report.missing`だけを反映。collection failureは捏造しない |
| 両方に回答可能なevidenceなし | `insufficient` |

全external taskが期間失敗かつevidenceが空の場合にEvidence Answer draftの一般的なmissingを抑制する
既存規則は維持する。content / response requirementの未達成と期間失敗自体は抑制しない。

## Implementation Surface

### Production

| 責務 | 主な追随先 |
|---|---|
| 2値type、draft、plan、正規化 | `app/agent/{contract.py,__init__.py}`、`app/agent/planning/{contract.py,__init__.py}` |
| prompt、wire schema、version | `app/agent/planning/{prompts.py,agent.py}`、`app/agent/planning/ai/schema_tool.py` |
| retry後停止、metric | `app/agent/planning/{service.py,failure.py,metrics.py}` |
| research goal語彙とQuery / Selector prompt version | `app/agent/evidence_collection/external_search/{contract.py,prompts.py}`とconsumer |
| 2 branch dispatch、期間伝播 | `app/agent/running/answering_runner.py` |
| plan summary、result invariant、external-only特例削除 | `app/agent/contract.py`、`app/agent/answering/result_assembly.py` |
| composition / probe / exports | `app/agent/composition.py`、`scripts/probe_question_answering.py`、関連`__init__.py` |
| worker error mapping回帰確認 | `app/queue/tasks/agent_run.py` |

production codeの変更でFastAPI schema、SQLAlchemy model、Alembic migration、frontend generated typeを
変更しない。共有`app.agent.contract`の変更はagent core内部の型変更であり、公開API response shapeの
破壊的変更ではない。

### Test Contract

実network、Gemini、DeepSeek、Tavily APIを呼ばないunit / integration testで、少なくとも次を保証する。

1. Contract
   - `QuestionPlan`が2 variantだけで、各discriminatorが`direct_answer | search`である。
   - validな`SearchPlan`はquery / taskを各1〜3件持ち、空・重複・4件以上を完成型で許可しない。
   - draft / completed plan / taskは`extra="forbid"`で旧fieldや未知fieldを受け付けない。
   - draftのstrip、空要素除去、重複除去、先頭3件への制限が決定的である。
   - SearchPlanの片側が正規化後0件、またはDirect draftのfield組み合わせが矛盾するとresponse defectになる。
   - `reason`、旧4値、旧variantをmodel / schemaが受け付けない。
2. Planner Agent
   - manual schemaが新field、2値enum、required配列を持ち、旧fieldを持たない。
   - promptが2択、query / goal責務、`target_time_window`規則を指示し、versionが`v3`である。
   - prompt inputのsanitize、untrusted block、safe repair hintを維持する。
3. External Query / Selector Agent
   - Query / Selectorのtask inputが`research_goal`を表示し、`collection_goal`を表示しない。
   - 固定input template変更に合わせ、両prompt versionが`v2`である。
   - query生成・candidate選別policy、sanitize、untrusted block、output schemaは変わらない。
4. Planning Service
   - attempt 1成功は1 call、response defect後の成功は2 callsで同じscopeを使う。
   - 最終response defectとretry不能provider errorはplanを返さず同じ分類済み例外を伝播する。
   - unknown exceptionとcancellationをfallbackへ変換しない。
   - Runtime scopeの開始・終了失敗ではoutcome metric、後続phase、完成plan返却が0回である。
   - Planner失敗時に`safe_fallback_plan`、internal search、external activation、answer agentが0回である。
   - metricの`planned` / `failed`、`retry_used`、`plan_type`、安全なfailure codeをscope退出後に
     exactly once保証する。
5. Answering Runner
   - Direct pathはretrieval 0回、Search pathは内部・外部を各1回実行する。
   - Searchの両枝が他方の完了前に開始し、両方の終了後にEvidence Answerへ進む。
   - `InternalSearchError`、external classified failure、片側unknown、両側unknownの優先規則を維持する。
   - external classified failureを`collection_failures`へ昇格させず、既存task reportのstatus / missingを
     保持する。
   - outer cancellationで両枝とexternal childを回収し、resource close後にcancellationを伝播する。
   - `target_time_window`を外部4箇所へ伝播し、内部strict filterへ渡さない。
6. Result Assembly
   - `plan_summary.plan_type`によるsource invariantと、direct planでcollection failureを拒否する契約を
     保証する。
   - external全taskの期間失敗について、internal evidence有無の両ケースが旧mixed semanticsになる。
   - external-only特例が残っていない。
7. Worker / Public Boundary
   - Plannerの分類済み最終失敗が`generation_unavailable`、unknownが`internal_error`になる。
   - Planner失敗後にassistant message / sourceが永続化されない。
   - 既存HTTP schema、stream event shape、frontend generated typeに差分がない。

主な追随testは次とする。

- `tests/agent/planning/test_contract.py`
- `tests/agent/planning/test_planner_agent_declaration.py`
- `tests/agent/planning/test_planner.py`
- `tests/agent/planning/test_planner_tracing.py`
- `tests/agent/running/test_retrieval_dispatch.py`
- `tests/agent/running/test_answering_workflow.py`
- `tests/agent/running/test_answering_runner.py`
- `tests/agent/running/test_external_pipeline.py`
- `tests/agent/evidence_collection/external_search/test_contract.py`
- `tests/agent/evidence_collection/external_search/test_agent_declaration.py`
- `tests/agent/answering/test_result_assembly.py`
- `tests/agent/answering/test_orchestration.py`
- `tests/agent/test_contract.py`
- `tests/agent/test_agent_run_task.py`
- `tests/scripts/test_probe_question_answering.py`

## Implementation

対応PR: [#47](https://github.com/yook11/Vector/pull/47)

実装commit:

- `b5e2f9b8`: Plannerの最終失敗時にfallbackせず回答runを停止する。
- `c29676f2`: 外部taskとQuery / Selector promptを`research_goal`語彙へ統一する。
- `63f7a4a8`: Planner、Runner、結果契約、probe、testをDirect / Searchの2 planへ移行する。
- `87abb2c8`: 公開仕様と保証台帳をDirect / Searchの現行契約へ更新する。
- `d6adcb42`: 未分類障害、`PlanType` SSoT、probeの型変換境界を厳密化する。
- `40fa5f22`: probeの不正な時刻指定を固定エラーへ変換し、入力の再表示を防ぐ。

中間的なhidden modeやfallbackをproductionへ残さず、2値contractと全consumerを同じ変更で
切り替えた。公開仕様とtest guarantee ledgerは別commitで現行契約へ更新した。

## Verification

### 仕様作成時

- 現行Planner contract、prompt、wire schema、service、metric、Runner、result assemblyを確認した。
- 内部queryのembedding / article search利用箇所と、research goalから外部queryを生成する下流境界を確認した。
- `target_time_window`のresolver、External Query Agent、Tavily、Evidence Answer Agentへの伝播を確認した。
- Query / Selectorのmodel-visibleなinput template変更が手動prompt version更新対象であることを確認した。
- plan type / retrieval summaryがDB、HTTP response、frontend generated typeへ投影されないことを確認した。
- production codeとtestは変更していないため、test suiteは未実行である。

### 実装完了時

- Planner / Runner / result / probeの対象test: `283 passed`。
- DB / Redisを起動するintegration suite: `980 passed, 22 skipped`。
- Ruff、format check、`git diff --check`: 成功。
- scoped searchで旧plan variant、旧field、fallback、旧summary / metric語彙のproduction consumerが
  残っていないことを確認した。
- `specs/agent`の公開仕様とtest guarantee ledgerを現行2 plan契約へ更新した。
- Gemini、DeepSeek、Tavilyと実DBを使うprobeは、秘密情報と実行環境が必要なため未実行。
