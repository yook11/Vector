# External search Tavily date filter slice 仕様

更新日: 2026-07-20

実装状況: Draft

前提slice:

- `question-answering-external-research-task-contract-slice.md`
- `external-search-research-runner-slice.md`
- `external-search-tavily-provider-slice.md`
- `external-search-deepseek-adapters-slice.md`
- `external-pipeline-ownership-slice.md`

## Positioning

本sliceは、question plannerが型付きの閉じた契約で返す検索期間を、run開始時に確定した
`as_of`と明示された日付に基づく絶対日付範囲へ1回だけ解決し、Tavily Search APIの
`start_date` / `end_date`へ渡す。

現在はplannerが`target_time_window: str | None`へ自由文を返し、その値はquery
generatorのpromptにだけ渡る。Tavily requestは常に日付条件なしであるため、
「今日」「直近24時間」「今週」等の明示的な鮮度要求があっても、古い候補が
検索poolへ混ざり得る。

本sliceでは次を同時に保証する。

1. 期間意図を自由文ではなく型付き値として確定する。
2. Tavily検索を`as_of`から解決した期間または明示された開始日〜終了日で絞る。
3. 意図的な期間なし、相対期間、暦期間、明示的な開始日〜終了日、未対応期間を
   重複のないkindとして表す。
4. 期間解決に失敗した場合は直近60日や無制限検索へsilent fallbackしない。
5. fail-closedによる検索全滅をユーザーと運用者の両方から観測可能にする。

Tavilyの日付条件は日単位で、responseの`published_at`が欠損する場合もある。
そのため期間外candidateを時刻精度で必ず除外する後段filterは本sliceに含めない。
また、本sliceが期間を適用するのはexternal searchだけである。`internal_and_external`でも
内部記事検索には期間filterを追加しないため、内部DB由来の古い記事が混ざる可能性は残る。
これは段階的な実装範囲として受容し、内部検索への期間適用は後続sliceで扱う。

`external-pipeline-ownership-slice.md`の実装により、external pipelineの実行ownerは
`AnsweringRunner`である。本sliceは削除済みの`ExternalSearchService`やnested runnerを
再導入せず、期間解決のdomain policyを`external_search` packageに、解決の実行と
Query Agent -> External Search Tool -> Selector Agentの進行を回答workflow ownerに置く。
既存Tool契約のうち「完成済みqueryを変更せず実行する」責務は維持し、
provider非依存の`date_filter`をtyped Tool inputへ追加する点だけを本sliceが更新する。

## Work definition

### Problem

1. `target_time_window`が自由文であり、planner promptの例示以外に出力の閉集合を
   構造的に保証するschemaがない。
2. `ExternalSearchToolInput`は`query`と`limit`しか持たず、Tavily request bodyへ
   日付条件を送れない。
3. 現行の`ResearchTaskReport.status`はexternal search内部の診断値であり、最終回答は
   `report.missing`しか読むことができない。
4. `ResearchTaskReport.status`はassistant message、DB、公開APIへ保存・投影されない。
5. plan共通の期間解決に失敗すると全external taskが失敗するが、そのままでは
   ユーザーに「根拠ゼロのinsufficient」としか見えず、運用者も原因を識別できない。
6. UTC暦日で「今日」「今週」「今月」を解決すると、日本時間の暦日とずれて
   対象期間の一部を取りこぼし得る。
7. 時間指定なし、曖昧な「最近」、期間解決失敗を同じ`None` / fallbackへ潰すと、
   古いニュースの混入または質問と異なる期間への置換が起きる。一方、同じ日付範囲へ
   解決される`latest`、`last_7_days`、`last_n_days(7)`等を別kindにすると、契約とtestが
   増えるだけでTavilyの絞り込み能力は増えない。
8. 期間なし検索は公式情報・歴史的背景・基準文書の発見に必要だが、最新ニュースの
   既定動作にすると古い結果が再発する。
9. 最終`missing_aspects`には根拠0件、collection failure、external task missing、
   Evidence Answer draft missing、回答要望の未達成という複数のproducerがある。
   期間失敗文言の重複排除を最終配列全体の1件制約とすると、期間失敗と無関係な
   回答形式・内容要望の未達成を消去し得る。
10. 新しい期間metricはvalidなtyped期間がexternal branchへ到達した後のresolver結果だけを
    観測する。planner schema validation failure後のinternal-only fallbackはその手前で発生するため、
    計測母集団を明記しないと、運用者が期間意図を満たせなかった質問の全量と誤解し得る。

### Evidence

- `app/agent/planning/contract.py`
  - `QuestionPlanDraft`とexternal plan 2 variantが同じ`str | None`を保持する。
- `app/agent/planning/prompts.py`
  - 「今日」「直近24時間」「今週」「2026年6月」等を例示するが、閉集合ではない。
- `app/agent/planning/ai/schema_tool.py`
  - nullable stringと英語例示だけで、enumや構造化年月を持たない。
- `app/agent/runtime/gemini.py`
  - structured outputでは`GenerateContentConfig.response_schema`へGemini用schemaを渡す。
- `app/agent/planning/service.py` / `metrics.py`
  - response validation failureを1回retryし、未回復なら`InternalRetrievalPlan`へfallbackする。
  - fallbackは既存`vector.agent.planner.outcome`へ記録されるが、失われたplanner出力が
    publication期間を意図していたかは判別できない。
- `app/agent/running/answering_runner.py`
  - external branchの丸め、task fan-out、Query Agent -> Tool -> Selector Agent、集約、
    dedupe、`ExternalSearchOutcome`構築を所有する。
  - planの自由記述期間とrunの`as_of`をexternal pipelineへ渡し、
    query generatorに期間を渡すが、Tool呼び出しは`query`と`limit`だけである。
- `app/agent/evidence_collection/external_search/contract.py`
  - `ExternalQueryGenerationInput`は自由記述期間を保持する。
  - `ExternalSearchToolInput`は`query`と`limit`だけを持ち、日付条件を受けない。
  - `ResearchTaskReport`はstatusを持つが、ユーザー向けprojectionではない。
- `app/agent/evidence_collection/external_search/policy.py`
  - query / candidate / evidenceの上限、丸め、失敗reason等の純粋なdomain policyを保持する。
- `app/agent/evidence_collection/external_search/deepseek_binding.py` / `prompts.py`
  - query generator prompt直前まで`str | None`を受ける。
- `app/agent/answering/result_assembly.py`
  - external task reportから最終回答へ取り込む値は現在`report.missing`だけである。
  - evidence 0件では`_RETRIEVAL_EMPTY_MISSING`を先に追加し、collection failureは
    `_COLLECTION_FAILURE_MISSING`で表示文言へ写像する。
  - Evidence Answerの`unfulfilled_requirement_ids`をcontent / response requirementsの固定文言へ
    写像し、`requirement_missing_aspects`としてdraft missingとは別に最終配列へ追加する。
- `app/agent/answering/evidence_answer/contract.py` / `flow.py`
  - Evidence Answer境界も`target_time_window: str | None`を受ける。
- `app/agent/answering/evidence_answer/prompts.py` / `agent.py`
  - Evidence Answerのprompt直前まで自由記述期間が伝搬する。
- `app/agent/runs/result_mapper.py`
  - `AnswerQuestionResult.missing_aspects`をassistant messageへ保存する。
- `app/agent/threads/projection.py`
  - 保存済み`missing_aspects`を公開assistant messageへ投影する。
- `app/agent/evidence_collection/external_search/tavily.py`
  - request bodyは`topic=news` / `search_depth=basic`等の固定値だけである。
- `tests/agent/evidence_collection/external_search/test_tavily.py`
  - 現在の日付条件なしpayloadをexact equalityで固定している。
  - `published_date`不明のcandidateを保持する契約がある。
- Tavily公式Search API:
  - `time_range`はproviderの現在日からの相対期間で、`day` / `week` /
    `month` / `year`を受ける。
  - `start_date` / `end_date`は`YYYY-MM-DD`で、publish dateまたはlast updated
    dateに基づいて結果を絞る。
  - startは「after」、endは「before」と説明される。
  - https://docs.tavily.com/documentation/api-reference/endpoint/search
- Tavily公式changelog:
  - `start_date` / `end_date`を同時利用でき、指定範囲を`strictly`と説明する。
  - https://docs.tavily.com/changelog
- Gemini公式Structured outputs:
  - structured outputがJSON Schemaのsubsetであり、未対応propertyは無視され得ると説明する。
  - 一般対応表とは別に、本repositoryの`google-genai==2.10.0`、
    `gemini-2.5-flash-lite`、legacy `response_schema`経路を実APIで確認する。
  - https://ai.google.dev/gemini-api/docs/generate-content/structured-output

### Invariants

1. planner期間出力の最終契約はPydantic response boundaryが所有し、未知kind、extra field、
   kind依存field条件を含む閉集合を保証する。Gemini response schemaは対象model・SDK・API経路で
   確認できたprovider-supported subsetだけを生成時制約として持ち、完全一致を前提にしない。
   自由記述の期間文字列はexternal search契約へ渡さない。
2. 日付範囲の基準時刻はrun開始時に確定済みのtimezone-aware `as_of`とし、
   resolver内で現在時刻を再取得しない。
3. naive `as_of`は呼び出し側の不変条件違反であり、期間解決失敗として握らず伝播する。
4. Tavilyのprovider現在時刻に依存する`time_range`は使用せず、
   `start_date` / `end_date`へ絶対日付を送る。
5. 時間指定なしでは日付keyを送らず、現在の検索挙動を維持する。
6. `target_time_window=None`はplannerが「publication期間を意図的に絞らない」と
   判断した場合だけを表し、期間解決失敗のfallback値として使用しない。
7. 「最新」は`last_n_days(7)`、「最近」は`last_n_days(60)`へplanner promptで正規化する。
   `last_24_hours`、`last_7_days`、`last_30_days`、`latest`、`recent_default`をkindとして
   重複定義しない。
8. `last_n_days(60)`は曖昧な鮮度要求に対するplannerの明示的な選択であり、
   resolver / external pipelineのfailure fallbackとして生成しない。
9. 期間filterは1回の回答runのexternal branch単位で1回だけ解決し、
   全task・全generated queryへ同じ値を渡す。
10. 意味的に適用できない期間を日付条件なし・直近60日検索へsilent fallbackしない。
11. fail-closed時はreportの診断status / reasonを正本とし、result assemblyが期間失敗の
   user-facing missing aspectへtask数にかかわらず重複なく1件だけ写像する。この1件制約は
   期間失敗文言そのものにだけ適用し、最終`missing_aspects`全体の件数を制限しない。
   external pipeline ownerは表示文言を所有しない。
   `requirement_missing_aspects`はcontent / responseのどちらも期間失敗と独立したproducerとして
   常に保持する。その他にも期間失敗と独立した不足理由があれば、最終配列は複数件を許容する。
12. fail-closed時は低cardinality reasonを持つmetricとstructured warningをexternal branch単位で
   1回だけ記録する。
13. `start_date` / `end_date`はPython `date`を内部正本とし、Tavily adapter境界でだけ
    ISO `YYYY-MM-DD`へ変換する。
14. 不正順序と、保守mappingに必要な前日を表現できない`date.min`開始の
    `ExternalSearchDateFilter`はVO構築時に拒否し、adapterへ到達させない。
15. 日付条件追加後もtask/query/candidate/evidenceの上限、打ち切り、並列、timeout、
    部分失敗の分類、provider rank、response整形の規則を変更しない。
16. API key、request body、response body、質問本文、対象年月日を新しいlog / metricへ記録しない。
17. DB schema、公開API response shape、認証・認可、frontend型を変更しない。
18. 期間filterの有無によって、既存のprovider部分失敗・全失敗・timeout分類を変えない。
19. 未対応の明示的なpublication期間を`None`、`last_n_days`、近似kindへ丸めない。
    ユーザーが開始日と終了日を明示し、両端を一意に確定できる連続した日単位の範囲は
    `date_range`として扱い、
    `unsupported_explicit_window`へ丸めない。質問対象時期はpublication期間と分離し、
    未対応扱いにしない。
20. 削除済みのnested runner、external search service、中継request / result DTOを再導入しない。
    期間解決は`external_search` packageの純粋なdomain policyとし、`AnsweringRunner`がexternal branchの
    実行ownerとして呼び出す。Toolへは解決済みfilterだけを渡し、`RunContext`やraw `as_of`を渡さない。
21. `external_search_time_filter_resolution_total`はvalidなtyped期間がexternal branchへ到達した後だけを
    計測し、planner schema validation failureを期間失敗へ推測変換しない。planner fallbackは既存metricの
    責務とし、新metric単独で期間問題の全量を表すとは保証しない。

### Non-goals

- `published_at`による時刻精度のcandidate後段filter。
- `published_at=None`のcandidateをdropする品質policy。
- internal retrievalへの期間filter適用。`internal_and_external`では内部DB由来の期間外記事が
  混ざり得ることを既知の限界として受容する。
- Tavily `time_range` / `auto_parameters`の利用。
- 検索結果の信頼性・権威性・一次情報判定。
- Tavily scoreの保持、ranking、semantic deduplication。
- Search → Filter → Extract、本文取得、claim verification。
- internal / external evidence統合後のcoverage評価。
- 不足根拠に基づく再検索loop。
- 時間指定なし質問へ一律のfreshness defaultを適用すること。
- 期間解決失敗を`last_n_days(60)`へ自動変換すること。
- recent newsと期間なし一次情報を同一runで別々の期間policyにすること。
- 公式・IR・規制当局・論文向けの専用検索レーン。
- userごとのtimezone設定。V1はproduct calendar timezoneを固定する。
- planner全体のfallback policy変更。
- 実Geminiを使ったラベル付き32問のsemantic accuracy評価と、production相当200問の
  unsupported率による出荷gate。
- 専用のexternal time-filter failure progress eventと、SSE / Redis / frontend event型の追加。
- 新規dependencyの追加。

### Done

- planner期間がPydantic response boundaryで型付き閉集合として検証され、自由文alias parserが
  存在しない。Gemini schemaは確認済みのprovider-supported subsetだけを持つ。
- 対応する期間を持つexternal searchの全Tavily callへ、typed期間と`as_of`から解決した
  `start_date` / `end_date`が送られる。
- 期間指定なしでは日付keyが送られず、既存payloadの他fieldが変わらない。
- 「最新」が`last_n_days(7)`、「最近」が`last_n_days(60)`、`None`が意図的な期間なしとして
  正規化される。
- 意味的に適用不能な期間はunbounded searchへ縮退しない。
- 期間解決失敗は`last_n_days(60)`へ縮退しない。
- fail-closed reportは固定reasonを持ち、external pipeline ownerでは表示用`missing`を持たない。
- result assemblyが複数のfail-closed reportを期間失敗missing aspect 1件へ写像し、その文言が
  `AnswerQuestionResult`、assistant message DB row、
  `ResearchAssistantMessage.missing_aspects`へ到達する。
- external-onlyの全task期間失敗では一般的な根拠0件文言とdraft missingの取込みを抑制し、
  期間失敗文言を1件だけ追加する。`requirement_missing_aspects`とその他の独立した不足理由は
  保持し、それらがない場合だけ最終missingを期間失敗文言1件にする。mixed planでも同じく
  独立した不足理由を保持する。
- fail-closed時のmetric / warningがraw期間や質問を含まず、external branch単位で1回だけ発生する。
- 新しい期間metricの計測母集団、planner fallbackの除外、既存planner outcome metricとの
  責務分担、および期間問題の全量を単独では測れない限界が明記される。
- naive `as_of`と不正VOが分類済み品質劣化へ変換されず、バグとして伝播する。
- `yesterday`、`last_week`、1〜60日の`last_n_days`、明示的な`date_range`を解決し、
  それ以外の未対応の明示publication期間を型付きsentinelでfail-closedして質問対象時期とは
  区別する。
- `None`経路でもresolverと`not_requested` metricがexternal branch単位で各1回、warningが0回となる。
- 非`None` filterありでもprovider部分失敗・全失敗・timeoutの既存分類が維持される。
- fail-closed時のexternal activity eventは0件である。
- pure resolver、VO、AnsweringRunner external pipeline、Tool contract、Tavily adapter、result assembly、
  planner prompt/schemaのtestが本仕様を固定する。
- 実Tavily probeを有限回で実行し、conclusiveなら観測した境界mapping、inconclusiveなら
  本仕様の保守的mappingを採用して結果を本仕様へ反映する。
- `/check`で該当backend検証がgreenになる。

## Design decisions

### 1. planner期間を型付き閉集合にする

`target_time_window: str | None`を廃止し、planning contractが次の値を所有する。

```python
TargetTimeWindowKind = Literal[
    "today",
    "yesterday",
    "last_n_days",
    "this_week",
    "last_week",
    "this_month",
    "calendar_month",
    "date_range",
    "unsupported_explicit_window",
]


class TargetTimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: TargetTimeWindowKind
    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    days: int | None = Field(default=None, ge=1, le=60)
    start_date: date | None = None
    end_date_inclusive: date | None = None
```

validatorは次を保証する。

- `calendar_month`では`year`と`month`が必須。
- `year`はPython `date`が表現できる1〜9999。
- `month`は1〜12。
- `last_n_days`では`days`が必須で1〜60。
- `date_range`では`start_date`と`end_date_inclusive`が必須で、
  `start_date <= end_date_inclusive < date.max`。開始日と終了日が同じ1日指定も受理する。
  JSON上はISO `YYYY-MM-DD`だけを受理し、Pydantic boundaryでPython `date`へ変換する。
- `calendar_month`以外では`year` / `month`は`None`。
- `last_n_days`以外では`days`は`None`。
- `date_range`以外では`start_date` / `end_date_inclusive`は`None`。
- `unsupported_explicit_window`は、明示的なpublication期間だがV1の対応範囲外であることを
  表す既知のsentinelであり、全parameterは`None`。
- 時間指定なしは`TargetTimeWindow`を作らず`None`。
- 未知kindとextra fieldはresponse boundaryで拒否する。

plannerのGemini response schemaは、少なくとも同じenum、year、month、days、start date、
inclusive end dateを持つnullable objectへ変更する。ただし、Pydantic contractとの完全一致は
前提にしない。2026-07-20の実API probeでは、現在の`google-genai==2.10.0`、
`gemini-2.5-flash-lite`、`GenerateContentConfig.response_schema`の組み合わせで
`additionalProperties`がHTTP 400となったため含めない。`anyOf`、`format: date`、nullableは
request受理を確認したが、kind依存の禁止fieldまでprovider保証に含めない。

- 対応を確認できた制約だけをGemini response schemaへ含める。
- extra field拒否やkind依存のrequired / prohibited fieldをprovider側で表現できない場合も、
  Pydantic validatorを緩めない。
- provider側で表現できない制約はPydantic response boundaryと既存1回retryが所有する。

Pydantic schema validation failureは既存planner retry契約へ入り、resolverの通常失敗にはしない。
retry後も未知kind等でvalidationに失敗した場合は、既存plannerの安全な
`InternalRetrievalPlan` fallbackへ入り、`time_filter_failed`へ変換しない。

planner promptはユーザーの言い回しをkindへ意味的に正規化する。

```text
「ここ1週間」「直近一週間」「最近7日間」「last 7 days」
  -> kind=last_n_days, days=7

「2026年6月1日から2026年6月15日まで」
  -> kind=date_range, start_date=2026-06-01, end_date_inclusive=2026-06-15
```

resolverは日本語・英語・aliasをparseしない。

### 2. 期間policyを明示的に選ぶ

plannerはpublication期間を次の4群として区別する。

| policy | planner値 | 用途 |
| --- | --- | --- |
| 意図的な期間なし | `None` | 歴史的背景、基準文書、公式情報の原典発見等 |
| 鮮度要求 | `last_n_days` | 「最新」は7日、「最近」は60日、明示された1〜60日の相対期間 |
| 解決可能な明示期間 | その他の対応済みkind | 今日、昨日、今週、先週、今月、具体月、開始日〜終了日 |
| 未対応の明示期間 | `unsupported_explicit_window` | 対応済みkindまたは明示された両端日付の`date_range`へ安全に正規化できない期間 |

`None`を選んでよい代表例:

- 公式ドキュメント・製品仕様の原典を探す。
- 規制・法律・標準仕様の本文を探す。
- 過去の決算資料・年次報告書・論文を探す。
- 買収・提携等の過去イベントや比較対象を探す。
- publication dateが質問の正しさに影響しない背景調査。

`None`を選んではならない代表例:

- 最新ニュース、直近の企業動向、最近の市場反応。
- 現在の投資判断へ影響する発表・規制・セキュリティ事象。
- ユーザーが新しさを要求しているが期間幅だけが曖昧な質問。

最後のケースは`last_n_days(days=60)`を選ぶ。期間指定のvalidation / resolution failureを
理由に`last_n_days(days=60)`へ変更してはならない。

よく使われる「昨日」「先週」「過去3日」はそれぞれ`yesterday`、`last_week`、
`last_n_days(days=3)`へ正規化する。「直近24時間」「直近7日」「直近30日」「最新」
「最近」も、それぞれ`last_n_days(days=1|7|30|7|60)`へ正規化する。可変N日は1〜60だけを
V1で解決可能とし、同じ範囲を表す別kindを追加しない。

「2026-04-01から2026-05-15」のように開始日と終了日を一意に確定できる明示的な連続範囲は
`date_range(start_date=2026-04-01, end_date_inclusive=2026-05-15)`とする。自然言語の「まで」は
終了日を含む意味としてplanner contractへ保持し、resolverが終了日の翌日を計算して内部半開区間へ
変換する。両端の年が省略されている場合は、会話文脈で対象年が一意な場合、または`as_of`の
JST暦年を補って`start_date <= end_date_inclusive <= as_ofのJST暦日`となり、他の年を示す文脈が
ない場合だけ`date_range`へ正規化する。年またぎ、片側だけの年省略、未来日になる補完、
複数の解釈が成立する場合は推測しない。

V1語彙外のpublication期間として明示された「前四半期」「2026年度に公開された資料」
「過去90日」、境界を一意に決められない「6月頃」、非連続な「6月と8月」等は
`unsupported_explicit_window`とする。`None`や最近傍kindへ丸めず、resolverで分類済み
fail-closedにする。一方、
「2026年度の業績見通し」のように年度が質問対象時期を表す場合は`None`とし、年度表現を
collection goal / queryへ残す。

`target_time_window`はplan共通であり、V1では「直近ニュースは7〜60日、公式原典は
期間なし」の2レーンを同一runで別々に実行できない。混合質問では鮮度要求を優先し、
必要な古い公式資料を取り逃がす可能性を既知の限界として受容する。task単位の期間policy
または公式情報専用レーンは後続sliceで扱い、混合質問を理由にrun全体をunboundedへ
広げない。

### 3. publication windowと質問対象時期を分離する

`TargetTimeWindow`は**外部根拠の公開・更新期間**だけを表す。

- 「直近1週間のNVIDIAニュース」: `last_n_days(7)`。
- 「2026年6月に公開された発表」: `calendar_month(2026, 6)`。
- 「2026年6月1日から15日までに公開された発表」:
  `date_range(2026-06-01, 2026-06-15 inclusive)`。
- 「2027年のAI市場予測」: 2027年は質問対象時期でありpublication windowではないため
  `target_time_window=None`。2027という語はcollection goal / queryへ残す。

future対象時期をpublication date filterへ誤適用しない規則をpromptへ明記し、Pydantic contractと
planner prompt/schemaの自動testで固定する。
開始日と終了日が明示され一意に確定できるpublication期間は`date_range`へ、対応済みkindまたは
明示された両端日付へ安全に正規化できないpublication期間は`unsupported_explicit_window`へ
正規化し、未知kindを生成しないことも固定する。

### 4. prompt境界では決定的な表示文字列へ変換する

planning / evidence collection / external searchの内部境界は`TargetTimeWindow | None`を
そのまま使う。

query generatorとevidence answer promptは現在文字列を受けるため、prompt rendererの
直前でplanning-owned helperにより決定的な表示文字列へ変換する。

```text
today                -> 今日
yesterday            -> 昨日
last_n_days(1)       -> 直近24時間
last_n_days(7)       -> 直近7日
this_week            -> 今週
last_week            -> 先週
last_n_days(3)       -> 直近3日
last_n_days(30)      -> 直近30日
last_n_days(60)      -> 直近60日
this_month           -> 今月
calendar_month(2026, 6) -> 2026年6月
date_range(2026-06-01, 2026-06-15) -> 2026年6月1日から2026年6月15日まで
unsupported_explicit_window -> 対応外の明示期間
```

LLMが生成したraw labelを再利用しない。同じhelperをquery generatorとanswer promptの
両方で使い、表示意味の二重定義を作らない。

### 5. product calendar timezoneをAsia/Tokyoに固定する

V1ではuser timezoneを追加せず、暦日系のproduct timezoneを
`ZoneInfo("Asia/Tokyo")`に固定する。`zoneinfo`は標準libraryであり新規dependencyを
追加しない。

- `today` / `yesterday` / `this_week` / `last_week` / `this_month` /
  `calendar_month`は、`as_of`をJSTへ変換して暦日境界を決める。
- `last_n_days`は、まず`as_of`から
  durationを引いたinstantを求め、その上下限をJST暦日へ投影してTavily用の
  date envelopeを作る。
- `date_range`の両端は日付としてすでに確定しているためtimezone変換せず、future判定と
  当日へのclipだけを`as_of`のJST暦日に基づいて行う。

これによりJST 00:00〜09:00とUTC暦日のずれによる「今日」の取りこぼしを避ける。
将来user timezoneを導入する場合は、このproduct timezoneを置換する別sliceで扱う。

### 6. `time_range`ではなく絶対日付を正本にする

Tavilyの`time_range`はTavily側の現在日を基準とする。Vectorはrun開始時に
`RunContext.as_of`を確定しているため、同じrunを遅延実行・再現しても期間が
変わらない絶対日付を正本とする。

```text
TargetTimeWindow + as_of
  -> ExternalSearchDateFilter(start_date, end_date)
  -> Tavily body {start_date: YYYY-MM-DD, end_date: YYYY-MM-DD}
```

`time_range`と絶対日付を同じrequestへ併用しない。

### 7. 日付filterはfrozen VOにする

`app/agent/evidence_collection/external_search/contract.py`にprovider非依存の値objectを追加する。

```python
class ExternalSearchDateFilter(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.start_date == date.min:
            raise ValueError("start_date must have a previous calendar day")
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        return self
```

内部契約は`[start_date, end_date)`の半開区間とする。

- `start_date`: 対象期間の開始暦日。
- `end_date`: 対象期間の最終日ではなく、対象期間直後の暦日。
- `None`: filterなし。
- raw dictやplanner出力をTavily adapterへ直接渡さない。

不正順序はVO構築時の不変条件違反である。`date.min`開始は、inconclusive probe後に採用する
開始日1日前への保守的拡張を表現できないため、VOのvalid範囲から除外する。resolverが
`calendar_month(1, 1)`または`date_range`の`0001-01-01`開始を受けた場合は、VO構築前に
`unexpandable_start_date`へ分類し、validation errorを漏らさない。

### 8. pure resolverはexternal branch単位で1回だけ呼ぶ

`app/agent/evidence_collection/external_search/time_filter.py`へI/Oを持たないpure functionを追加する。

```python
def resolve_external_search_date_filter(
    target_time_window: TargetTimeWindow | None,
    *,
    as_of: datetime,
) -> ExternalSearchDateFilter | None: ...
```

resolverは次を守る。

- timezone-aware `as_of`だけを受ける。
- naive `as_of`は`ValueError`等のprogramming errorとして伝播する。
- `datetime.now()`を呼ばない。
- `TargetTimeWindow.kind`をexhaustiveに処理し、未知kindのfallbackを作らない。
- plan共通の期間をtaskごとに解決せず、`AnsweringRunner`のexternal branch入口で
  task fan-outより前に1回だけ解決する。private methodへのexactな分割は固定しない。
- Tavily、HTTP、query、candidate、logを知らない。

### 9. V1期間変換

`as_of = 2026-07-12T00:30:00Z = 2026-07-12T09:30:00+09:00`の例:

| kind | 意味 | start_date | end_date |
| --- | --- | --- | --- |
| `None` | 時間指定なし | なし | なし |
| `today` | JST当日 | 2026-07-12 | 2026-07-13 |
| `yesterday` | JST前日 | 2026-07-11 | 2026-07-12 |
| `last_n_days(1)` | 直前24時間を覆うJST日付 | 2026-07-11 | 2026-07-13 |
| `last_n_days(7)` | 直前7日を覆うJST日付 | 2026-07-05 | 2026-07-13 |
| `this_week` | JST月曜からas_ofまで | 2026-07-06 | 2026-07-13 |
| `last_week` | 直前の完了済みJST月曜〜日曜 | 2026-06-29 | 2026-07-06 |
| `last_n_days(3)` | 直前3日を覆うJST日付 | 2026-07-09 | 2026-07-13 |
| `last_n_days(30)` | 直前30日を覆うJST日付 | 2026-06-12 | 2026-07-13 |
| `last_n_days(60)` | 曖昧な「最近」の直近60日 | 2026-05-13 | 2026-07-13 |
| `this_month` | JST月初からas_ofまで | 2026-07-01 | 2026-07-13 |
| `calendar_month(2026, 6)` | 指定暦月 | 2026-06-01 | 2026-07-01 |
| `date_range(2026-06-01, 2026-06-15 inclusive)` | 明示された両端包含範囲 | 2026-06-01 | 2026-06-16 |
| `date_range(2026-07-01, 2026-07-31 inclusive)` | future側をas_of当日へclip | 2026-07-01 | 2026-07-13 |

`calendar_month`のpolicy:

- 過去月: 月初から翌月初まで。
- as_ofを含む当月: 月初からas_of翌日までにclipする。
- as_ofより後の月: publication windowとして適用不能とし、
  `future_calendar_month`でfail-closedする。

`date_range`のpolicy:

- planner contractの`start_date`と`end_date_inclusive`は両端包含である。
- resolverは`end_date_inclusive + 1日`により内部の半開区間へ変換する。
- 終了日がas_ofのJST暦日より後なら、内部`end_date`をas_of JST暦日の翌日へclipする。
- 開始日がas_ofのJST暦日より後ならpublication windowとして適用不能とし、
  `future_date_range`でfail-closedする。
- Pydantic boundaryで開始日が終了日より後の範囲と、翌日を表現できない
  `end_date_inclusive=date.max`を拒否する。resolverで反転範囲を補正しない。

`yesterday`は暦日前日であり、直前24時間の`last_n_days(1)`とは同じ意味にしない。
`last_week`は当週途中を含めず、直前に完了したJST月曜〜日曜とする。`last_n_days`は
`as_of`からN日を引いたinstantをJST date envelopeへ投影する。

V1では明示的な任意日付範囲を`date_range`で扱うが、四半期、年度、61日以上の相対期間、
曖昧な境界、非連続期間は追加しない。これらがpublication期間として明示された場合は
`unsupported_explicit_window`へ正規化する。解決可能kindの追加は、本sliceの実装条件や
実質問率の出荷gateにはせず、後続の要求として別途判断する。

### 10. fail-closed reasonとerror型を閉集合にする

```python
TimeFilterFailureReason = Literal[
    "future_calendar_month",
    "future_date_range",
    "unexpandable_start_date",
    "unsupported_explicit_window",
]


class ExternalSearchDateFilterResolutionError(Exception):
    reason: TimeFilterFailureReason
```

resolverはfuture `calendar_month`、future `date_range`、開始日前日を表現できない範囲、
`unsupported_explicit_window`を
この分類済みerrorで通知し、`AnsweringRunner`のexternal pipeline ownerはこの型だけをcatchして
`time_filter_failed` reportへ変換する。

`invalid_month`、未知kind、extra fieldはplanner/Pydantic response boundaryで拒否する。
resolverに到達した未知kindは実装のexhaustiveness違反であり、
`unsupported_time_window`へ丸めずバグとして伝播する。

`ResearchTaskStatus`へ`time_filter_failed`、`ResearchTaskReport`へ
`time_filter_failure_reason: TimeFilterFailureReason | None`を追加する。

report validatorは次を保証する。

- `time_filter_failed`ではreason必須。
- `time_filter_failed`では次の診断値を厳密に固定する。
  - `generated_queries=[]`
  - `provider_failed_query_count=0`
  - `candidate_count=0`
  - `evidence_count=0`
  - `dropped_selection_count=0`
  - `selector_failure_reason=None`
  - `missing=[]`
  - `time_filter_failure_reason`は必須
- その他statusでは`time_filter_failure_reason=None`。

`ExternalSearchDateFilterResolutionError`の`str()`、`args`、`repr()`は、class名等の
固定表現を除き、可変情報として固定reason codeだけを含む。対象year / month / days / dates、
raw planner出力、質問本文等をmessageへ埋め込まない。

### 11. ユーザー向け可視化

`AnsweringRunner`のexternal pipeline ownerは診断値だけをoutcomeへ格納し、
`app/agent/answering/result_assembly.py`が`ResearchTaskReport.status`を表示文言へ写像する。
既存の`_COLLECTION_FAILURE_MISSING`と同じconsumer-side mapping patternで、例えば次の
閉じた写像を所有する。

```python
_EXTERNAL_TASK_STATUS_MISSING = {
    "time_filter_failed": "指定された公開期間を外部検索へ適用できませんでした",
}
```

表示する固定文言:

```text
指定された公開期間を外部検索へ適用できませんでした
```

rawの年月、planner出力、provider名、内部reason codeをユーザー文言へ含めない。

複数taskが同じstatusでも、result assemblyは期間失敗文言を1件だけ追加する。

```text
ResearchTaskReport.status / time_filter_failure_reason
  -> result assemblyのstatus-to-missing mapping
  -> AnswerQuestionResult.missing_aspects
     （期間失敗文言は1件、配列全体は独立producerがあれば複数件）
  -> AgentMessage.missing_aspects（DB）
  -> ResearchAssistantMessage.missing_aspects（API / UI）
```

missingの組立policyを次で固定する。

- 「1件だけ」という制約は、複数task由来の期間失敗文言にだけ適用する。
- `requirement_missing_aspects`はcontent / response requirementsのどちらに由来する場合も常に保持する。
  result assemblyで文言化した後に、期間失敗と意味的に独立しているかを再判定しない。
- 最終配列は既存のcanonical順序と文言単位のdeduplicationを維持する。

1. external-onlyで全external taskが`time_filter_failed`、他のcollection failureがなく、
   evidenceが0件:
   - `_RETRIEVAL_EMPTY_MISSING`は期間失敗と原因が重なるため抑制する。
   - Evidence Answer draftのmissingは根拠0件から生成された一般化であるため、
     最終配列へ取り込まない。
   - 期間失敗文言を1件だけ追加し、`requirement_missing_aspects`は保持する。
   - `requirement_missing_aspects`とその他の独立producerが空の場合だけ、最終配列は
     期間失敗文言1件となる。
2. internal+externalでinternal evidenceが1件以上ある:
   - `_RETRIEVAL_EMPTY_MISSING`は元から追加されない。
   - 期間失敗文言を1件追加し、internal evidenceから独立に判明したdraft missingは保持する。
   - `requirement_missing_aspects`も保持する。
3. internal+externalでinternal evidenceも0件:
   - external期間失敗だけでは全体の根拠0件を説明し切れないため、
     `_RETRIEVAL_EMPTY_MISSING`と期間失敗文言の2件を許容する。
   - Evidence Answer draftのmissingは根拠0件から生成された一般化であるため、
     最終配列へ取り込まない。
   - internal collection自体も失敗した場合は、`_COLLECTION_FAILURE_MISSING`の内部検索文言を
     独立した原因として追加する。
   - `requirement_missing_aspects`も保持する。

このmissing aspectにより、internal evidenceが得られた場合でも最終statusは
`insufficient`となり、指定期間を外部検索へ適用できなかった事実を隠さない。

公開API shapeやDB schemaは既存fieldを使うため変更しない。

### 12. 運用者向け可視化

回答runのexternal branch単位のresolver結果をmetricへ記録する。

```text
external_search_time_filter_resolution_total{
  result="not_requested|resolved|failed",
  reason="none|future_calendar_month|future_date_range|unexpandable_start_date|unsupported_explicit_window"
}
```

fail-closed時だけstructured warningを1回記録する。

```text
event=external_search_time_filter_failed
reason=future_calendar_month|future_date_range|unexpandable_start_date|unsupported_explicit_window
task_count=<bounded integer>
```

- taskごとに同じwarningを重複記録しない。
- target year / month / days / start date / inclusive end date、raw planner output、query、質問、URLを
  記録しない。
- metric/log失敗を検索結果へ影響させない既存observability方針に従う。

このmetricの計測母集団は、plannerがvalidなtyped planを返し、external branchが開始された
回答runだけである。planner response validation failureが1回retry後も未回復の場合は、既存の
`InternalRetrievalPlan` fallbackへ入るためresolverへ到達せず、このmetricもwarningも記録しない。

planner fallback自体は既存の`vector.agent.planner.outcome`で`result=fallback`、
`retry_used`、閉じた`failure_code`として観測する。ただし、その失敗したraw出力がpublication
期間を意図していたかは安全に判別できない。したがって、両metricを合わせても
「期間意図を満たせなかった質問の全量」は算出できず、新しい期間metricを全量指標として使わない。

### 13. fail-closed時のexternal pipeline結果

`AnsweringRunner`のexternal pipeline ownerはtask fan-out前に期間を1回解決する。

```text
resolution success
  -> query generationをtask並列実行
  -> 全ExternalSearchToolInputへ同じdate_filterを渡す

resolution failure(future_calendar_month | future_date_range | unexpandable_start_date | unsupported_explicit_window)
  -> metric / warningを1回
  -> 全task分のtime_filter_failed reportを構築
  -> 各report.missingは空、status / reasonだけを保持
  -> query generator / Tool / selectorは呼ばない
```

`ExternalSearchOutcome`が要求するtask/report対応は維持する。reportはtask数分作るが、
resolver、metric、warningはexternal branch単位で1回だけである。

`target_time_window=None`でもresolverはexternal branch単位で1回呼び、`None`を返す。
`result=not_requested, reason=none` metricを1回記録し、resolved / failed metricとwarningは
記録しない。

fail-closed時は既存のexternal activity eventである`queries_generated`、
`candidates_fetched`、`evidence_selected`を1件もemitしない。専用eventは本sliceでは
追加せず、全体stageは既存どおり`retrieving`から`synthesizing`へ進み、最終結果の
missing aspectで失敗を可視化する。

### 14. External Search Tool / Tavily mapping

`ExternalSearchToolInput`は全callerへfilter有無の明示を要求する。
Toolは完成済みqueryと上限に加えてprovider非依存の解決済みfilterだけを受け、
`TargetTimeWindow`、`as_of`、`RunContext`を受けない。

```python
@dataclass(frozen=True, slots=True)
class ExternalSearchToolInput:
    query: str
    limit: int
    date_filter: ExternalSearchDateFilter | None


class ExternalSearchTool(Protocol):
    @property
    def name(self) -> ExternalSearchToolName: ...

    async def invoke(
        self,
        input: ExternalSearchToolInput,
    ) -> list[ExternalSearchCandidate]: ...
```

filterありmappingは2026-07-20の有限probeを`inconclusive`として終了し、
取りこぼし回避を優先する保守mappingへ確定した。request bodyの形は次とする。

```python
{
    "query": query,
    "topic": "news",
    "search_depth": "basic",
    "max_results": min(limit, TAVILY_MAX_RESULTS_LIMIT),
    "include_answer": False,
    "include_raw_content": False,
    "start_date": provider_start_date.isoformat(),
    "end_date": provider_end_date.isoformat(),
}
```

- `provider_start_date = date_filter.start_date - 1日`。
- `provider_end_date = date_filter.end_date`。
- valid VOは開始日の前日を必ず表現できるため、adapterでclampやfallbackを行わない。

filterなし:

- `start_date` / `end_date` / `time_range`をbodyへ含めない。
- `null`を送らない。
- 既存固定fieldを変更しない。

adapterはvalid VOをTavily表現へ写像するだけで、日付順序や期間意味を再検証しない。

### 15. 日単位prefilterの限界

Tavilyへ送れる条件は日単位である。「直近24時間」を正規化した`last_n_days(1)`では
取りこぼしを避けるため、
下限instantを含むJST日からas_of JST日翌日までの広いenvelopeを送る。この結果、
24時間より少し古いcandidateが返る可能性は残る。

またTavilyはpublish dateまたはlast updated dateをfilter基準とする一方、responseの
`published_date`は欠損し得る。したがって本sliceの保証は次に限定する。

```text
保証する: Tavily検索をas_ofから解決した期間または明示された日付範囲で絞る
保証しない: 全candidateが時刻精度で対象期間内と証明される
保証しない: internal retrievalのcandidateが同じ期間内に限定される
```

厳密保証が必要な場合は後続sliceで、datetimeの正確な内部windowとcandidate
`published_at`を比較する。その際に`published_at=None`をdropするか、
「鮮度未確認」として別扱いにするかを品質policyとして決める。

### 16. Tavily境界日はprobeで確定する

公式APIとchangelogはstartを「after」、endを「before」、範囲を`strictly`と説明する。
一方、date-only値をどの時刻・timezoneで評価するか、start日当日とend日当日の
包含挙動は文面だけでは完全に確定できない。

probeでは次を独立して確認する。

1. start日当日の既知記事が含まれるか。
2. end日当日の既知記事が含まれるか。
3. 時刻・timezone付き`published_date`を日付条件へどう丸めるか。
4. Tavilyの日付比較がUTC、別timezone、またはprovider独自基準のどれか。
5. internal半開区間をそのまま送るか、adapter境界で補正が必要か。

probeの有限停止条件:

- start境界とend境界について、それぞれ公開日時と最終更新日時の両方をpublisherの
  machine-readable metadata等、Tavily responseとは独立した根拠で確認できる既知記事を最大3件使う。
- 確定判定に使えるfixtureは、公開日時と最終更新日時のどちらをTavilyが採用しても
  同じ包含・除外結論になるものに限る。両日時が同一なのは十分条件だが必須ではない。
- 片方の日時が不明、更新日表示がないだけ、または両日時が検証境界の異なる側にあるfixtureは
  補助観測にだけ使い、境界規則の確定根拠にしない。
- 各fixtureはfilterなしで取得可能なことを先に確認し、filterありを最大2回試す。
- `max_results=20`を使い、rankに現れない記事だけで包含・除外を断定しない。
- start境界とend境界をそれぞれ適格なfixtureで直接確認し、adapter mappingを一意に決められる場合だけ
  全体を`conclusive`とする。片側だけ確認できた状態をmapping全体の確定扱いにしない。
- 全fixture・全試行で判定できなければ`inconclusive`として打ち切り、無期限に再試行しない。

`conclusive`では観測結果に合わせてadapter mappingを確定する。`inconclusive`でも本slice
全体はblockせず、取りこぼし回避を優先して次の保守的mappingを採用する。

```text
provider start_date = internal start_date - 1日
provider end_date   = internal end_date
```

これは公式の`after` / `before`記述に対しstart対象日を確実にenvelopeへ含めるための
design-time fallbackであり、runtime fallbackや設定分岐ではない。providerがstart日を
inclusiveに扱う場合は開始側に最大1 provider暦日古い結果が、end日をinclusiveに扱う場合は
終了側に最大1 provider暦日新しい結果が混ざり得る。比較timezoneを確定できない場合はさらに
JST境界とのずれがあり得るため、「JST基準で最大24時間」とは保証しない。本filterを厳密な
後段検証ではなく、古い方向・新しい方向の両方に過包含し得る粗いprovider prefilterとして扱う。
#### 2026-07-20 実API probe結果

- 使用経路: repository設定のTavily Search API、`topic=news`、`search_depth=basic`、
  `max_results=20`。raw response、API key、記事本文は保存していない。
- 非機密query要約: OpenAI GPT-5.6発表に関するニュース検索。
- baseline上位20件に現れ、publisher pageのmachine-readable metadataから公開日時と
  最終更新日時をTavily responseとは独立に確認できた3記事をfixtureとした。

| fixture | publisher metadataで確認した日時 | start境界観測 | end境界観測 |
| --- | --- | --- | --- |
| AP配信記事 | 公開 2026-07-17 18:28:36Z、更新は同日20:06〜20:07Z | 当日指定・前日指定とも出現 | 当日指定で不出現、翌日指定で出現 |
| TechCrunch記事 | 公開 2026-07-15 00:27:04Z、更新は同日00:49:30Z | 当日指定・前日指定とも出現 | 当日指定で不出現、翌日指定で出現 |
| NBC記事 | 公開・更新とも2026-07-18 00:00:00Z | 当日指定・前日指定とも出現 | 当日指定で出現、翌日指定で不出現 |

公開日時と更新日時は各fixtureで同じ暦日側にあり、どちらをTavilyが採用しても境界判定の
前提は変わらない。一方、NBCのend側は範囲を広げた翌日指定で順位内から消える非単調な結果となり、
`max_results=20`のranking変動と境界除外を分離できなかった。date-only比較timezoneもresponseから
観測できなかった。そのため包含規則・比較timezoneは`inconclusive`で有限停止し、
`start-1日 / endそのまま`を採用mappingとして確定した。

このprobeはcandidate全件の時刻精度適合を保証しない。`published_date`欠損やenvelope外candidateは
後段dropの根拠にせず、既存どおり保持する。

## Error contract

| 条件 | 挙動 |
| --- | --- |
| `target_time_window=None` | 意図的な期間なしとしてfilterなし検索 |
| 「最新」→`last_n_days(7)` | 直近7日の絶対日付へ解決して検索 |
| 「最近」→`last_n_days(60)` | 直近60日の絶対日付へ解決して検索 |
| valid `date_range` | 両端包含のplanner値を内部半開区間へ変換して検索。future側だけならas_of当日へclip |
| 対応済みkind | 絶対日付へ解決して検索 |
| `unsupported_explicit_window` | 全task `time_filter_failed`、status-to-missing写像、metric/log |
| future `calendar_month` | 全task `time_filter_failed`、status-to-missing写像、metric/log |
| future `date_range` | 全task `time_filter_failed`、status-to-missing写像、metric/log |
| startが`date.min`となる月・日付範囲 | `unexpandable_start_date`で全task `time_filter_failed`、status-to-missing写像、metric/log |
| 期間validation / resolution failure | `last_n_days(60)` / `None`へfallbackしない |
| naive `as_of` | programming errorとして伝播 |
| planner未知kind / invalid month / invalid days / invalid date range | response validation failure → 1回repair → 未回復なら既存internal-only fallback。既存planner outcome metricだけを記録し、`time_filter_failed`と期間metric / warningにはしない |
| resolver未知kind | exhaustiveness違反として伝播 |
| `start_date >= end_date` | VO構築時に拒否し、バグとして伝播 |
| `ExternalSearchDateFilter.start_date == date.min` | VO構築時に拒否。resolver到達可能入力は上記typed failureへ分類 |
| Tavily HTTP / timeout | 既存のprovider query失敗分類 |
| candidate `published_at`欠損 | 既存どおりcandidateを保持 |

## Test priority and source of truth

実装は各stepでtestを先に追加し、新しい保証についてredを確認してから実装する。

### P0: 本sliceの正しさとfail-closed

1. Pydanticが所有する型付き`TargetTimeWindow`の閉集合と、確認済みprovider-supported subsetだけを
   持つGemini schema。
2. JST基準のresolver、昨日・先週・可変N日・60日default・月・明示日付範囲・年・timezone境界。
3. `ExternalSearchDateFilter` VOの順序保証と、開始日前日を表現できる範囲保証。
4. external branch単位resolver 1回と、全task / queryへの同一filter伝搬。
5. future month / future date range / unsupported explicit windowのtyped fail-closed、downstream未呼び出し、
   naive `as_of`伝播。
6. 非`None` filter伝搬時のprovider部分失敗・全失敗・timeout分類の回帰防止。
7. Tavily filtered / unfiltered payloadのHTTP contract。
8. external-only / mixed plan別のstatus-to-missing写像、requirement missingとの併存、
   DB永続化、既存API投影。
9. metric / warningのexternal branch単位1回、閉じた属性、機密値非漏洩、sink失敗非影響、
   planner fallbackを含まない計測母集団。
10. Tavily start/end境界と比較timezoneの有限回実API probe。inconclusive時は保守的mappingを
    採用して無期限にblockしない。

### P1: 意味品質と表示一貫性

1. 全kindのdeterministic表示。
2. query generator / evidence answer promptへ同じ期間意味が渡ること。
3. plan draftからcompleted planへのtyped値保持。
4. planner prompt/schemaが`None`、`last_n_days`へ正規化する鮮度表現、`date_range`を含む
   対応期間、未対応の明示publication期間、質問対象時期を別概念として宣言すること。

### P2: 過剰または脆いため追加しないtest

- 全kind × 全timezone × 全月の直積。
- `AnsweringRunner`のprivate method間、Tool、Tavily adapter、result assemblyの全層で
  同じdate値を重複assertするtest。
- `datetime.now`未使用をsource scanやmonkeypatchで確認するtest。
- helper共有を関数identityやprivate call順で確認するtest。
- Logfire等のobservability基盤が付加する標準fieldを含むlog record完全一致。
- 実TavilyのURL・順位・件数をCIでexact assertするtest。
- `model_construct`で不正VOを作りadapterへ渡すtest。

既存testで保証済みの次は、fake signature / fixtureを新contractへ追従させるだけとし、
期間専用の重複testを増やさない。

- `AnsweringRunner`の期間 / `as_of`伝搬とexternal例外伝播。
- external pipelineの並列上限、query cap、provider部分・全失敗、timeout、selector retry、pool rank。
- TavilyのBearer、max results、HTTP/JSON error、candidate mapping、日付parse、unknown日付保持。
- result assemblyのmissing順序・dedupe・missingによる`insufficient`。
- 保存済み`missing_aspects`から公開`missingAspects`へのAPI投影。

## Test design

### Planning contract / schema

1. `yesterday`、`last_week`、`last_n_days`、`date_range`、
   `unsupported_explicit_window`を含む全kindを受理し、未知kindを拒否する。
2. `calendar_month`だけyear / month、`last_n_days`だけdays、`date_range`だけ
   start date / inclusive end dateを要求する。
3. year 0 / 10000、month 0 / 13、days 0 / 61、不正なISO日付、開始日が終了日より後、
   `end_date_inclusive=date.max`、各required field欠損、extra fieldを拒否する。
4. 他kindのyear / month / days / start date / inclusive end dateを拒否する。
5. Gemini schemaは対象model・SDK・`response_schema`経路で確認できたenum、数値範囲、
   date format、extra field拒否、kind分岐のsubsetだけを持つ。provider側で表現できない制約に
   ついてPydantic contractとの一致を要求しない。
6. planner promptがpublication windowと質問対象時期を分離する。
7. planner prompt/schemaが「直近24時間」「直近7日」「直近30日」「最新」「最近」を
   `last_n_days(1|7|30|7|60)`へ正規化し、重複kindを宣言しない。
8. planner prompt/schemaが両端を一意に確定できる開始日〜終了日を`date_range`、年を安全に
   補完できない範囲、曖昧な境界、非連続期間、V1未対応期間を
   `unsupported_explicit_window`として区別する。
9. plan draftからcompleted planへtyped windowをそのまま渡す。
10. deterministic表示helperが全kindを網羅する。
11. 未知kindはresponse validation failureとなり、1回repair後も未回復なら既存の
   `InternalRetrievalPlan` fallbackへ入り、resolverや`time_filter_failed`へ到達しない。
   既存planner outcome metricはfallbackを記録し、新しい期間metric / warningは記録しない。

### Date filter VO / resolver

1. `start_date < end_date`を受理する。
2. same / reverse dateをVO構築時に拒否する。
3. `None`がfilterなしになる。
4. `today` / `yesterday` / `this_week` / `last_week` / `this_month`をJST暦日で解決し、
   `yesterday`と`last_n_days(1)`、`last_week`と`this_week`を区別する。
5. `last_n_days(1|3|7|30|60)`をinstant起点で計算し、JST date envelopeへ投影する。
6. UTCでは前日だがJSTでは当日となるas_of境界を固定する。
7. `as_of=2026-07-31T15:30:00Z`（JST 2026-08-01 00:30）で、`this_month`が
   2026-08-01〜2026-08-02、`calendar_month(2026, 7)`が過去月全範囲、
   `calendar_month(2026, 8)`がfutureではなく当月clipになることを固定する。
8. `calendar_month`の過去月、当月clip、12月、閏年2月を固定する。
9. `date_range`の両端包含から内部半開区間への変換、同日1日範囲、future側clipを固定する。
10. start dateがas_of JST暦日より後の`date_range`を
    `ExternalSearchDateFilterResolutionError`の`future_date_range`へ分類する。
11. future monthを`ExternalSearchDateFilterResolutionError`の
   `future_calendar_month`へ分類する。
12. `unsupported_explicit_window`を同errorの同名reasonへ分類する。
13. errorの`str()`、`args`、`repr()`が、class名等の固定表現を除いて固定reason codeだけを
    含み、year / month / dates、質問、sentinel用のprobe文字列を含まないことを固定する。
14. naive as_ofが分類済みerrorではなく伝播する。
15. 未知kindのfallbackが存在しないことをexhaustivenessで固定する。

### External pipeline / report

1. `ResearchTaskReport`直接構築testで`time_filter_failed`の全field不変条件を固定し、
   いずれか1つでも非既定値なら拒否する。
2. filterあり・`None`・fail-closedの各経路でresolverがexternal branch単位で1回だけ呼ばれる。
3. 解決済みfilterが全task・全queryへ同値で渡る。
4. filterなしでは全`ExternalSearchToolInput`へ`None`が渡り、Tavilyの既存payload規則を変えない。
5. query generatorにはdeterministic表示文字列が渡る。
6. 「最新」は`last_n_days(7)`、「最近」は`last_n_days(60)`、`None`はfilterなしとして伝搬する。
7. valid `date_range`は両端包含から解決した同じfilterを全taskへ伝搬する。
8. future month、future date range、`unsupported_explicit_window`では全task分の
   `time_filter_failed` reportを返す。
9. fail-closedではquery generator / Tool / selectorを呼ばない。
10. 各fail-closed reportは閉じたreasonと全field既定値を持ち、表示文言を持たない。
11. `time_filter_failed`以外のreportがreasonを持つことを拒否する。
12. naive `as_of`がreportへ丸められず伝播し、failed metric / warningを作らない。
13. **非`None` filterがToolへ渡る状態で**provider部分失敗、全失敗、timeoutの既存分類と
    report集約が回帰しない。既存失敗testをfilter付きparameterへ更新して正本とし、
    同じ分類だけを確認する重複testは作らない。
14. fail-closed時に`queries_generated`、`candidates_fetched`、`evidence_selected` activity
    eventが0件であり、専用eventもemitしない。

### User / persistence visibility

1. external-onlyで全taskが`time_filter_failed`、evidence 0件では一般的な根拠0件を抑制し、
   draft missingを取り込まず、他の独立producerがない場合は期間失敗文言1件だけにする。
2. 前項でcontent / response requirementが未達成の場合は、期間失敗文言を1件だけ追加したうえで
   `回答要望を満たせませんでした: <description>`をcontextのcanonical順序で保持する。
   Evidence Answer draft missingの抑制はrequirement missingに適用しない。
3. 複数taskが`time_filter_failed`でrequirement missingが併存しても、期間失敗文言は1件であり、
   最終配列全体はrequirement missingの数に応じて複数件を許容する。
4. internal+externalでinternal evidenceがある場合は期間失敗文言を1件追加し、独立した
   draft missingを保持して`insufficient`にする。
   requirement missingも常に保持する。
5. internal+externalでinternal evidenceも0件の場合はdraft missingを取り込まず、
   一般的な根拠0件と期間失敗文言の2件を許容する。internal collection failureがあれば
   その固定文言とrequirement missingを追加し、期間失敗文言が1回だけであることを固定する。
6. repository integrationでcompleted runの`AgentMessage.missing_aspects`へ期間失敗文言が
   永続化される。
7. thread projection / APIは既存の任意missing投影testを再利用し、固定文言専用の
   route testを追加しない。
8. 公開API shapeを追加せず既存`missingAspects`で見える。

### Operator visibility

1. resolved / not requested / failed metricをexternal branch単位で1回記録する。
2. `target_time_window=None`ではresolver 1回、`not_requested` metric 1回、
   resolved / failed metric 0回、warning 0回である。
3. fail-closed warningを1回だけ記録する。
4. metric labelとwarning fieldが閉集合である。
5. raw期間、year、month、days、start date、inclusive end date、query、質問、URL、API keyを含めない。
6. observability sink失敗を検索失敗へ昇格させない。
7. planner response validation failure後のinternal-only fallbackでは期間metric / warningが0件であり、
   既存planner outcome metricがfallbackを記録する。期間意図だったかを新metricへ推測記録しない。

metric不在assertionでは、既存`tests/logfire/_metric_helpers.py`の
`collected_metrics(capfire)`を1回だけ読み、metric collectionが0件のときも空listとして
扱う。複数readやzero-metric時のcollector例外へtestを依存させない。

### Tavily adapter

1. valid filterをprobeで確定したprovider境界へ写像し、ISO `start_date` / `end_date`で送る。
2. filterありpayloadに`time_range`がない。
3. filterなしpayloadは日付3 keyを含まず、既存fieldと一致する。
4. max results、Bearer header、HTTP error、invalid JSON、candidate mapping、
   `published_date` parseの既存testがgreenのまま。

不正順序filterをadapterへ渡すtestは書かない。順序検証はVO testが所有する。
Tavily固有のpayload、開始日補正、`time_range`非使用、filterなし完全一致は
`test_tavily.py`へ集約する。`test_contract.py`はprovider非依存のVO / Tool portだけを所有する。

### Real API probe

既存`backend/scripts/probe_tavily_search.py`を拡張するか、日付filter専用probeを追加する。

- 同じqueryをfilterなし / filterありで実行する。
- start / end各境界につき最大3件、公開日時と最終更新日時を独立確認でき、どちらを基準にしても
  同じ境界判定になる既知ニュースを使う。filterありは各fixture最大2回とする。
- 片方の日時が不明、更新日表示がないだけ、または両日時が境界の異なる側にあるfixtureは
  `conclusive`判定に使わない。
- `max_results=20`でrank未出現を除外証明として扱わない。
- Tavilyがdate-only境界を比較するtimezoneを観測し、確定不能なら`inconclusive`と記録する。
- `published_date`欠損率と指定envelopeから明らかに外れた結果を確認する。
- raw response、API key、記事本文は保存・commitしない。
- 実測日、queryの非機密な要約、判定に使った公開日時・最終更新日時とその確認手段の要約、
  包含規則、比較timezoneの結論だけを本仕様へ追記する。
- start / endの片側でも適格なfixtureで直接確認できない場合や、上限内で結果が検索rankに
  現れない場合は`inconclusive`で打ち切る。
- `conclusive`では観測mapping、`inconclusive`では`start-1日 / endそのまま`の
  保守的mappingを採用し、どちらでも有限回のprobe後に実装確定へ進める。

## Implementation surface

```text
backend/app/agent/planning/contract.py
backend/app/agent/planning/prompts.py
backend/app/agent/planning/ai/schema_tool.py
backend/app/agent/running/answering_runner.py
backend/app/agent/evidence_collection/external_search/contract.py
backend/app/agent/evidence_collection/external_search/time_filter.py       # new
backend/app/agent/evidence_collection/external_search/metrics.py           # new
backend/app/agent/evidence_collection/external_search/tavily.py
backend/app/agent/evidence_collection/external_search/__init__.py
backend/app/agent/evidence_collection/external_search/prompts.py
backend/app/agent/answering/result_assembly.py
backend/app/agent/answering/evidence_answer/contract.py
backend/app/agent/answering/evidence_answer/flow.py
backend/app/agent/answering/evidence_answer/prompts.py

backend/tests/agent/planning/test_contract.py
backend/tests/agent/planning/test_planner.py
backend/tests/agent/planning/test_planner_agent_declaration.py
backend/tests/agent/evidence_collection/external_search/test_contract.py
backend/tests/agent/evidence_collection/external_search/test_time_filter.py # new
backend/tests/agent/evidence_collection/external_search/test_external_search_tool_contract.py
backend/tests/agent/evidence_collection/external_search/test_tavily.py
backend/tests/agent/evidence_collection/external_search/test_agent_declaration.py
backend/tests/agent/running/test_external_pipeline.py
backend/tests/agent/running/test_external_pipeline_tracing.py
backend/tests/agent/running/test_retrieval_dispatch.py
backend/tests/agent/answering/test_result_assembly.py
backend/tests/agent/answering/test_orchestration.py              # workflow integration regression
backend/tests/agent/answering/evidence_answer/test_flow.py
backend/tests/agent/answering/evidence_answer/ai/test_prompt_schema.py
backend/tests/agent/live_updates/test_answer_delta_integration.py
backend/tests/agent/test_agent_run_task.py                    # missing永続化回帰
backend/tests/agent/test_router_research.py                   # existing projection回帰
backend/scripts/probe_tavily_search.py                        # probe拡張候補
```

`target_time_window`の型変更後に`rg target_time_window backend/app backend/tests`を実行し、
上記以外に残った`str | None` consumer / fake / fixtureも同じ型またはprompt直前の
deterministic rendererへ追従させる。存在しない並行packageを新設しない。

既存fieldをそのまま使うため、次は読み取り正本または既存回帰testだけを利用し、
production変更対象にしない。

```text
backend/app/agent/runs/result_mapper.py
backend/app/agent/threads/projection.py
```

変更しない面:

- `backend/app/schemas/research.py`
- DB model / Alembic migration
- frontend generated types / UI
- authentication / authorization
- answer response / citation shape

## Behavior

```text
planner output:
  target_time_window={kind: "last_n_days", days: 1}

input.as_of=2026-07-12T00:30:00Z
  -> AnsweringRunner external branch resolver（1回）
       product timezone=Asia/Tokyo
       start_date=2026-07-11
       end_date=2026-07-13
  -> task/query fan-out
  -> 全ExternalSearchToolInputへ同じdate_filter
  -> 全Tavily callへ同じstart_date / end_date
  -> existing pool / selector / evidence path

planner output:
  target_time_window=None
  -> plannerがpublication期間なしを意図的に選択
  -> resolver 1回、returns None
  -> not_requested metric 1回、resolved / failed metric 0回、warning 0回
  -> Tavily body has no date keys

planner output:
  target_time_window={kind: "yesterday"}
  -> JST前日のdate filter

planner output:
  target_time_window={kind: "last_n_days", days: 3}
  -> 直前3日を覆うdate filter

planner output:
  target_time_window={kind: "last_n_days", days: 7}
  <- 「最新」をplannerが正規化
  -> 直近7日のdate filter

planner output:
  target_time_window={kind: "last_n_days", days: 60}
  <- 「最近」をplannerが正規化
  -> 直近60日のdate filter

planner output:
  target_time_window={
    kind: "date_range",
    start_date: "2026-06-01",
    end_date_inclusive: "2026-06-15"
  }
  -> ExternalSearchDateFilter(start_date=2026-06-01, end_date=2026-06-16)
  -> 全Tavily callへ同じ解決済みfilter

planner response validation failure（未知kind / invalid month / invalid date range等）
  -> 1回repair
  -> 未回復なら既存InternalRetrievalPlan fallback
  -> resolver / time_filter_failedへ到達しない
  -> 既存planner outcome metricへfallbackを記録
  -> 新しい期間metric / warningは記録しない

planner output:
  target_time_window={kind: "unsupported_explicit_window"}
  -> unsupported_explicit_window
  -> None / last_n_days / 近似kindへ置換しない
  -> 分類済みfail-closed

planner output:
  target_time_window={kind: "calendar_month", year: 2027, month: 1, days: null}
input.as_of=2026-07-12T00:30:00Z
  -> future_calendar_month
  -> metric / warningを1回
  -> 全task status=time_filter_failed
  -> 全report.missing=[]、reason=future_calendar_month
  -> external-onlyではresult assemblyがstatusを期間失敗文言1件へ写像し、
     一般的な根拠0件とdraft missingの取込みを抑制
  -> content / response requirement missingがあれば、期間失敗文言とは別に保持
  -> userのmissingAspectsへ「指定された公開期間を外部検索へ適用できませんでした」を
     1件表示・DB保存し、独立したrequirement missingも併存させる
  -> query generator / Tool / selectorは未呼び出し
  -> external activity eventは0件

input.as_of=naive datetime
  -> programming errorとして伝播
```

## Open verification items

1. recent newsと期間なし一次情報を同一runで扱う後続のtask単位期間policy。

## Completed verification items

1. 2026-07-20のGemini実API probe:
   - `google-genai==2.10.0`、`gemini-2.5-flash-lite`、legacy `response_schema`経路では、
     `additionalProperties`はgeneration configの未知fieldとしてHTTP 400で拒否された。
   - `anyOf`、`format: date`、nullableは有限probeでrequest受理を確認したが、禁止fieldや
     kind依存条件の完全強制まではprovider保証に含めない。
   - current full `QUESTION_PLANNER_GEMINI_SCHEMA`はrequest受理とvalid `date_range`生成を確認した。
   - よってextra field禁止とkind依存fieldはPydanticを最終SSoTとし、provider schemaへ
     `additionalProperties`を入れない。
2. 2026-07-20のTavily実API probe:
   - start/end包含規則とdate-only比較timezoneはranking変動から分離できず`inconclusive`で有限停止した。
   - adapter mappingは`start-1日 / endそのまま`へ確定した。詳細はDesign decision 16に記録した。
