# External search Tavily date filter slice 仕様

更新日: 2026-07-13

実装状況: Draft

前提slice:

- `question-answering-external-research-task-contract-slice.md`
- `external-search-research-runner-slice.md`
- `external-search-tavily-provider-slice.md`
- `external-search-deepseek-adapters-slice.md`

## Positioning

本sliceは、question plannerが型付きの閉じた契約で返す検索期間を、run開始時に
確定した`as_of`基準の絶対日付範囲へ1回だけ解決し、Tavily Search APIの
`start_date` / `end_date`へ渡す。

現在はplannerが`target_time_window: str | None`へ自由文を返し、その値はquery
generatorのpromptにだけ渡る。Tavily requestは常に日付条件なしであるため、
「今日」「直近24時間」「今週」等の明示的な鮮度要求があっても、古い候補が
検索poolへ混ざり得る。

本sliceでは次を同時に保証する。

1. 期間意図を自由文ではなく型付き値として確定する。
2. Tavily検索を`as_of`由来の日付範囲で絞る。
3. 意図的な期間なし、通常の最新、曖昧な最近、明示期間を別のkindとして表す。
4. 期間解決に失敗した場合は直近60日や無制限検索へsilent fallbackしない。
5. fail-closedによる検索全滅をユーザーと運用者の両方から観測可能にする。

Tavilyの日付条件は日単位で、responseの`published_at`が欠損する場合もある。
そのため期間外candidateを時刻精度で必ず除外する後段filterは本sliceに含めない。

## Work definition

### Problem

1. `target_time_window`が自由文であり、planner promptの例示以外に出力の閉集合を
   構造的に保証するschemaがない。
2. `SearchProvider.search()`は`query`と`limit`しか受けず、Tavily request bodyへ
   日付条件を送れない。
3. 現行の`ResearchTaskReport.status`はexternal search内部の診断値であり、最終回答は
   `report.missing`しか読むことができない。
4. `ResearchTaskReport.status`はassistant message、DB、公開APIへ保存・投影されない。
5. plan共通の期間解決に失敗すると全external taskが失敗するが、そのままでは
   ユーザーに「根拠ゼロのinsufficient」としか見えず、運用者も原因を識別できない。
6. UTC暦日で「今日」「今週」「今月」を解決すると、日本時間の暦日とずれて
   対象期間の一部を取りこぼし得る。
7. 時間指定なし、曖昧な「最近」、期間解決失敗を同じ`None` / fallbackへ潰すと、
   古いニュースの混入または質問と異なる期間への置換が起きる。
8. 期間なし検索は公式情報・歴史的背景・基準文書の発見に必要だが、最新ニュースの
   既定動作にすると古い結果が再発する。

### Evidence

- `app/agent/planning/contract.py`
  - `QuestionPlanDraft`とexternal plan 2 variantが同じ`str | None`を保持する。
- `app/agent/planning/ai/prompts.py`
  - 「今日」「直近24時間」「今週」「2026年6月」等を例示するが、閉集合ではない。
- `app/agent/planning/ai/schema_tool.py`
  - nullable stringと英語例示だけで、enumや構造化年月を持たない。
- `app/agent/evidence_collection/service.py`
  - planの期間と`AnswerQuestionInput.as_of`をexternal searchへ渡す。
- `app/agent/evidence_collection/contract.py`
  - `ExternalPlanSearcher`が自由記述期間を受ける。
- `app/agent/evidence_collection/external_search/contract.py`
  - `ExternalSearchRequest`は`as_of`と自由記述期間を保持する。
  - `SearchProvider.search()`は日付条件を受けない。
  - `ResearchTaskReport`はstatusを持つが、ユーザー向けprojectionではない。
- `app/agent/evidence_collection/external_search/service.py`
  - plan共通の自由記述期間をrequestへ詰めてrunnerへ渡す。
- `app/agent/evidence_collection/external_search/runner.py`
  - query generatorへ期間を渡すが、provider呼び出しはqueryとlimitだけである。
  - 分類済み境界errorとtimeoutだけを捕捉し、未分類例外は伝播する方針である。
- `app/agent/evidence_collection/external_search/ai/deepseek.py` / `prompts.py`
  - query generator prompt直前まで`str | None`を受ける。
- `app/agent/answering/orchestration.py`
  - external task reportから最終回答へ取り込む値は現在`report.missing`だけである。
  - evidence 0件では`_RETRIEVAL_EMPTY_MISSING`を先に追加し、collection failureは
    `_COLLECTION_FAILURE_MISSING`で表示文言へ写像する。
- `app/agent/answering/evidence_answer/contract.py` / `flow.py`
  - Evidence Answer境界も`target_time_window: str | None`を受ける。
- `app/agent/answering/evidence_answer/ai/gemini.py` / `prompt.py`
  - Evidence AnswerのGemini prompt直前まで自由記述期間が伝搬する。
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

### Invariants

1. plannerの期間出力はPydantic modelとGemini response schemaの閉集合で保証し、
   自由記述の期間文字列をexternal search契約へ渡さない。
2. 日付範囲の基準時刻はrun開始時に確定済みのtimezone-aware `as_of`とし、
   resolver内で現在時刻を再取得しない。
3. naive `as_of`は呼び出し側の不変条件違反であり、期間解決失敗として握らず伝播する。
4. Tavilyのprovider現在時刻に依存する`time_range`は使用せず、
   `start_date` / `end_date`へ絶対日付を送る。
5. 時間指定なしでは日付keyを送らず、現在の検索挙動を維持する。
6. `target_time_window=None`はplannerが「publication期間を意図的に絞らない」と
   判断した場合だけを表し、期間解決失敗のfallback値として使用しない。
7. `latest`は直近7日、`recent_default`は直近60日とし、両者を同じ意味にしない。
8. `recent_default`は曖昧な鮮度要求に対するplannerの明示的な選択であり、
   resolver / runnerのfailure fallbackとして生成しない。
9. 期間filterはrequest単位で1回だけ解決し、全task・全generated queryへ同じ値を渡す。
10. 意味的に適用できない期間を日付条件なし・直近60日検索へsilent fallbackしない。
11. fail-closed時はreportの診断status / reasonを正本とし、orchestrationが期間失敗の
   user-facing missing aspectへ重複なく1回だけ写像する。runnerは表示文言を所有しない。
   最終`missing_aspects`全体は、期間失敗と独立した不足理由があれば複数件を許容する。
12. fail-closed時は低cardinality reasonを持つmetricとstructured warningをrequest単位で
   1回だけ記録する。
13. `start_date` / `end_date`はPython `date`を内部正本とし、Tavily adapter境界でだけ
    ISO `YYYY-MM-DD`へ変換する。
14. 不正順序の`ExternalSearchDateFilter`はVO構築時に拒否し、adapterへ到達させない。
15. 日付条件追加後もtask/query/candidate/evidenceの上限、打ち切り、並列、timeout、
    部分失敗の分類、provider rank、response整形の規則を変更しない。
16. API key、request body、response body、質問本文、年月を新しいlog / metricへ記録しない。
17. DB schema、公開API response shape、認証・認可、frontend型を変更しない。
18. 期間filterの有無によって、既存のprovider部分失敗・全失敗・timeout分類を変えない。
19. 未対応の明示的なpublication期間を`None`、`latest`、`recent_default`、近似kindへ
    丸めない。質問対象時期はpublication期間と分離し、未対応扱いにしない。

### Non-goals

- `published_at`による時刻精度のcandidate後段filter。
- `published_at=None`のcandidateをdropする品質policy。
- Tavily `time_range` / `auto_parameters`の利用。
- 検索結果の信頼性・権威性・一次情報判定。
- Tavily scoreの保持、ranking、semantic deduplication。
- Search → Filter → Extract、本文取得、claim verification。
- internal / external evidence統合後のcoverage評価。
- 不足根拠に基づく再検索loop。
- 時間指定なし質問へ一律のfreshness defaultを適用すること。
- 期間解決失敗を`recent_default`へ自動変換すること。
- recent newsと期間なし一次情報を同一runで別々の期間policyにすること。
- 公式・IR・規制当局・論文向けの専用検索レーン。
- userごとのtimezone設定。V1はproduct calendar timezoneを固定する。
- planner全体のfallback policy変更。
- 専用のexternal time-filter failure progress eventと、SSE / Redis / frontend event型の追加。
- 新規dependencyの追加。

### Done

- planner期間が型付き閉集合で生成・検証され、自由文alias parserが存在しない。
- 対応する期間を持つexternal searchの全Tavily callへ、`as_of`由来の
  `start_date` / `end_date`が送られる。
- 期間指定なしでは日付keyが送られず、既存payloadの他fieldが変わらない。
- `latest`が直近7日、`recent_default`が直近60日、`None`が意図的な期間なしとして
  区別される。
- 意味的に適用不能な期間はunbounded searchへ縮退しない。
- 期間解決失敗は`recent_default`へ縮退しない。
- fail-closed reportは固定reasonを持ち、runnerでは表示用`missing`を持たない。
- orchestrationが複数のfail-closed reportを期間失敗missing aspect 1件へ写像し、その文言が
  `AnswerQuestionResult`、assistant message DB row、
  `ResearchAssistantMessage.missing_aspects`へ到達する。
- external-onlyの全task期間失敗では一般的な根拠0件文言とdraft missingの取込みを抑制し、
  最終missingを期間失敗文言1件にする。mixed planでは独立した不足理由を保持する。
- fail-closed時のmetric / warningがraw期間や質問を含まず、request単位で1回だけ発生する。
- naive `as_of`と不正VOが分類済み品質劣化へ変換されず、バグとして伝播する。
- planner期間evalで構造違反0件を確認し、意味的なkind選択正解率と
  future対象時期の誤filter率を記録する。
- planner期間evalが意図的な期間なし、`latest`、`recent_default`を区別する。
- external-search対象のsanitized質問200件以上でunsupported率2%以下を確認し、超過時は
  頻出期間kindを追加するまで出荷しない。
- `yesterday`、`last_week`、1〜60日の`last_n_days`を解決し、それ以外の未対応の明示
  publication期間を型付きsentinelでfail-closedして質問対象時期とは区別する。
- `None`経路でもresolverと`not_requested` metricがrequest単位で各1回、warningが0回となる。
- 非`None` filterありでもprovider部分失敗・全失敗・timeoutの既存分類が維持される。
- fail-closed時のexternal activity eventは0件である。
- pure resolver、VO、runner、Tavily adapter、orchestration、result mapping、
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
    "last_24_hours",
    "last_7_days",
    "this_week",
    "last_week",
    "last_30_days",
    "last_n_days",
    "this_month",
    "latest",
    "recent_default",
    "calendar_month",
    "unsupported_explicit_window",
]


class TargetTimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: TargetTimeWindowKind
    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    days: int | None = Field(default=None, ge=1, le=60)
```

validatorは次を保証する。

- `calendar_month`では`year`と`month`が必須。
- `year`はPython `date`が表現できる1〜9999。
- `month`は1〜12。
- `last_n_days`では`days`が必須で1〜60。
- `calendar_month`以外では`year` / `month`は`None`。
- `last_n_days`以外では`days`は`None`。
- `unsupported_explicit_window`は、明示的なpublication期間だがV1の対応範囲外であることを
  表す既知のsentinelであり、`year` / `month` / `days`は`None`。
- 時間指定なしは`TargetTimeWindow`を作らず`None`。
- 未知kindとextra fieldはresponse boundaryで拒否する。

plannerのGemini response schemaも同じenum、year、month、daysを持つnullable objectに変更する。
schema validation failureは既存planner retry契約へ入り、resolverの通常失敗にはしない。
retry後も未知kind等でschema validationに失敗した場合は、既存plannerの安全な
`InternalRetrievalPlan` fallbackへ入り、`time_filter_failed`へ変換しない。

planner promptはユーザーの言い回しをkindへ意味的に正規化する。

```text
「ここ1週間」「直近一週間」「最近7日間」「last 7 days」
  -> kind=last_7_days
```

resolverは日本語・英語・aliasをparseしない。

### 2. 期間policyを明示的に選ぶ

plannerはpublication期間を次の5群として区別する。

| policy | planner値 | 用途 |
| --- | --- | --- |
| 意図的な期間なし | `None` | 歴史的背景、基準文書、公式情報の原典発見等 |
| 通常の最新 | `latest` | 「最新」「直近のニュース」。直近7日 |
| 曖昧な最近 | `recent_default` | 時間感覚は新しさ重視だが明示期間なし。直近60日 |
| 明示期間 | その他の解決可能kind | 今日、昨日、24時間、7日、今週、先週、可変N日、30日、今月、具体月 |
| 未対応の明示期間 | `unsupported_explicit_window` | 四半期、年度、任意日付範囲等 |

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

最後のケースは`recent_default`を選ぶ。期間指定のvalidation / resolution failureを
理由に`recent_default`へ変更してはならない。

よく使われる「昨日」「先週」「過去3日」はそれぞれ`yesterday`、`last_week`、
`last_n_days(days=3)`へ正規化する。可変N日は1〜60だけをV1で解決可能とし、7日と30日は
plannerが原則として`last_7_days` / `last_30_days`を選ぶ。resolverは契約内の
`last_n_days(days=7|30)`も同じ意味で決定的に解決する。

V1語彙外のpublication期間として明示された「前四半期」「2026年度に公開された資料」
「2026-04-01から2026-05-15」「過去90日」等は`unsupported_explicit_window`とする。`None`や
最近傍kindへ丸めず、resolverで分類済みfail-closedにする。一方、
「2026年度の業績見通し」のように年度が質問対象時期を表す場合は`None`とし、年度表現を
collection goal / queryへ残す。

`target_time_window`はplan共通であり、V1では「直近ニュースは7〜60日、公式原典は
期間なし」の2レーンを同一runで別々に実行できない。混合質問では鮮度要求を優先し、
必要な古い公式資料を取り逃がす可能性を既知の限界として受容する。task単位の期間policy
または公式情報専用レーンは後続sliceで扱い、混合質問を理由にrun全体をunboundedへ
広げない。

### 3. publication windowと質問対象時期を分離する

`TargetTimeWindow`は**外部根拠の公開・更新期間**だけを表す。

- 「直近1週間のNVIDIAニュース」: `last_7_days`。
- 「2026年6月に公開された発表」: `calendar_month(2026, 6)`。
- 「2027年のAI市場予測」: 2027年は質問対象時期でありpublication windowではないため
  `target_time_window=None`。2027という語はcollection goal / queryへ残す。

future対象時期をpublication date filterへ誤適用しないことをpromptとevalで固定する。
語彙外の明示publication期間は`unsupported_explicit_window`へ正規化し、未知kindを
生成しないことも固定する。

### 4. prompt境界では決定的な表示文字列へ変換する

planning / evidence collection / external searchの内部境界は`TargetTimeWindow | None`を
そのまま使う。

query generatorとevidence answer promptは現在文字列を受けるため、prompt rendererの
直前でplanning-owned helperにより決定的な表示文字列へ変換する。

```text
today                -> 今日
yesterday            -> 昨日
last_24_hours        -> 直近24時間
last_7_days          -> 直近7日
this_week            -> 今週
last_week            -> 先週
last_30_days         -> 直近30日
last_n_days(3)       -> 直近3日
this_month           -> 今月
latest               -> 最新（直近7日）
recent_default       -> 最近（直近60日）
calendar_month(2026, 6) -> 2026年6月
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
- `last_24_hours` / `last_7_days` / `last_30_days` / `latest` /
  `last_n_days` / `recent_default`は、まず`as_of`から
  durationを引いたinstantを求め、その上下限をJST暦日へ投影してTavily用の
  date envelopeを作る。

これによりJST 00:00〜09:00とUTC暦日のずれによる「今日」の取りこぼしを避ける。
将来user timezoneを導入する場合は、このproduct timezoneを置換する別sliceで扱う。

### 6. `time_range`ではなく絶対日付を正本にする

Tavilyの`time_range`はTavily側の現在日を基準とする。Vectorは質問受付時に
`AnswerQuestionInput.as_of`を確定しているため、同じrunを遅延実行・再現しても期間が
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
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        return self
```

内部契約は`[start_date, end_date)`の半開区間とする。

- `start_date`: 対象期間の開始暦日。
- `end_date`: 対象期間の最終日ではなく、対象期間直後の暦日。
- `None`: filterなし。
- raw dictやplanner出力をTavily adapterへ直接渡さない。

不正順序はVO構築時の不変条件違反である。分類済み期間失敗へ変換せず伝播させる。

### 8. pure resolverはrequest単位で1回だけ呼ぶ

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
- plan共通の期間をtaskごとに解決せず、runnerのrequest入口で1回だけ解決する。
- Tavily、HTTP、query、candidate、logを知らない。

### 9. V1期間変換

`as_of = 2026-07-12T00:30:00Z = 2026-07-12T09:30:00+09:00`の例:

| kind | 意味 | start_date | end_date |
| --- | --- | --- | --- |
| `None` | 時間指定なし | なし | なし |
| `today` | JST当日 | 2026-07-12 | 2026-07-13 |
| `yesterday` | JST前日 | 2026-07-11 | 2026-07-12 |
| `last_24_hours` | 直前24時間を覆うJST日付 | 2026-07-11 | 2026-07-13 |
| `last_7_days` | 直前7日を覆うJST日付 | 2026-07-05 | 2026-07-13 |
| `this_week` | JST月曜からas_ofまで | 2026-07-06 | 2026-07-13 |
| `last_week` | 直前の完了済みJST月曜〜日曜 | 2026-06-29 | 2026-07-06 |
| `last_30_days` | 直前30日を覆うJST日付 | 2026-06-12 | 2026-07-13 |
| `last_n_days(3)` | 直前3日を覆うJST日付 | 2026-07-09 | 2026-07-13 |
| `this_month` | JST月初からas_ofまで | 2026-07-01 | 2026-07-13 |
| `latest` | product defaultの直近7日 | 2026-07-05 | 2026-07-13 |
| `recent_default` | 曖昧な最近の直近60日 | 2026-05-13 | 2026-07-13 |
| `calendar_month(2026, 6)` | 指定暦月 | 2026-06-01 | 2026-07-01 |

`calendar_month`のpolicy:

- 過去月: 月初から翌月初まで。
- as_ofを含む当月: 月初からas_of翌日までにclipする。
- as_ofより後の月: publication windowとして適用不能とし、
  `future_calendar_month`でfail-closedする。

`yesterday`は暦日前日であり、直前24時間の`last_24_hours`とは同じ意味にしない。
`last_week`は当週途中を含めず、直前に完了したJST月曜〜日曜とする。`last_n_days`は
`as_of`からN日を引いたinstantをJST date envelopeへ投影する。

V1では任意日付範囲、四半期、年度は追加しない。これらがpublication期間として
明示された場合は`unsupported_explicit_window`へ正規化する。代表質問における
unsupported率が出荷gateを超える場合は、fail-closedのまま出荷せず解決可能kindを増やす。

### 10. fail-closed reasonとerror型を閉集合にする

```python
TimeFilterFailureReason = Literal[
    "future_calendar_month",
    "unsupported_explicit_window",
]


class ExternalSearchDateFilterResolutionError(Exception):
    reason: TimeFilterFailureReason
```

resolverはfuture `calendar_month`と`unsupported_explicit_window`をこの分類済みerrorで
通知し、runnerはこの型だけをcatchして`time_filter_failed` reportへ変換する。

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
固定表現を除き、可変情報として固定reason codeだけを含む。対象year / month / days、raw planner
出力、質問本文等をmessageへ埋め込まない。

### 11. ユーザー向け可視化

runnerは診断値だけを返し、orchestrationが`ResearchTaskReport.status`を表示文言へ写像する。
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

複数taskが同じstatusでも、orchestrationは期間失敗文言を1件だけ追加する。

```text
ResearchTaskReport.status / time_filter_failure_reason
  -> orchestrationのstatus-to-missing mapping
  -> AnswerQuestionResult.missing_aspects（期間失敗文言は1件）
  -> AgentMessage.missing_aspects（DB）
  -> ResearchAssistantMessage.missing_aspects（API / UI）
```

missingの組立policyを次で固定する。

1. external-onlyで全external taskが`time_filter_failed`、他のcollection failureがなく、
   evidenceが0件:
   - `_RETRIEVAL_EMPTY_MISSING`は期間失敗と原因が重なるため抑制する。
   - Evidence Answer draftのmissingは根拠0件から生成された一般化であるため、
     最終配列へ取り込まない。
   - 最終`missing_aspects`は期間失敗文言1件だけとする。
2. internal+externalでinternal evidenceが1件以上ある:
   - `_RETRIEVAL_EMPTY_MISSING`は元から追加されない。
   - 期間失敗文言を1件追加し、internal evidenceから独立に判明したdraft missingは保持する。
3. internal+externalでinternal evidenceも0件:
   - external期間失敗だけでは全体の根拠0件を説明し切れないため、
     `_RETRIEVAL_EMPTY_MISSING`と期間失敗文言の2件を許容する。
   - Evidence Answer draftのmissingは根拠0件から生成された一般化であるため、
     最終配列へ取り込まない。
   - internal collection自体も失敗した場合は、`_COLLECTION_FAILURE_MISSING`の内部検索文言を
     独立した原因として追加する。

このmissing aspectにより、internal evidenceが得られた場合でも最終statusは
`insufficient`となり、指定期間を外部検索へ適用できなかった事実を隠さない。

公開API shapeやDB schemaは既存fieldを使うため変更しない。

### 12. 運用者向け可視化

request単位のresolver結果をmetricへ記録する。

```text
external_search_time_filter_resolution_total{
  result="not_requested|resolved|failed",
  reason="none|future_calendar_month|unsupported_explicit_window"
}
```

fail-closed時だけstructured warningを1回記録する。

```text
event=external_search_time_filter_failed
reason=future_calendar_month|unsupported_explicit_window
task_count=<bounded integer>
```

- taskごとに同じwarningを重複記録しない。
- target year / month / days、raw planner output、query、質問、URLを記録しない。
- metric/log失敗を検索結果へ影響させない既存observability方針に従う。

### 13. fail-closed時のrunner結果

runnerはtask fan-out前に期間を1回解決する。

```text
resolution success
  -> query generationをtask並列実行
  -> providerへ同じdate_filterを渡す

resolution failure(future_calendar_month | unsupported_explicit_window)
  -> metric / warningを1回
  -> 全task分のtime_filter_failed reportを構築
  -> 各report.missingは空、status / reasonだけを保持
  -> query generator / provider / selectorは呼ばない
```

`ExternalSearchOutcome`が要求するtask/report対応は維持する。reportはtask数分作るが、
resolver、metric、warningはrequest単位で1回だけである。

`target_time_window=None`でもresolverはrequest単位で1回呼び、`None`を返す。
`result=not_requested, reason=none` metricを1回記録し、resolved / failed metricとwarningは
記録しない。

fail-closed時は既存のexternal activity eventである`queries_generated`、
`candidates_fetched`、`evidence_selected`を1件もemitしない。専用eventは本sliceでは
追加せず、全体stageは既存どおり`retrieving`から`synthesizing`へ進み、最終結果の
missing aspectで失敗を可視化する。

### 14. SearchProvider / Tavily mapping

`SearchProvider`は全callerへfilter有無の明示を要求する。

```python
class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        limit: int,
        date_filter: ExternalSearchDateFilter | None,
    ) -> list[ExternalSearchCandidate]: ...
```

filterありmappingはprobe後に確定する。request bodyの形は次とする。

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

- probeがconclusive: 観測した包含規則に従ってinternal半開区間をprovider日付へ写像する。
- probeがinconclusive: `provider_start_date = date_filter.start_date - 1日`、
  `provider_end_date = date_filter.end_date`とする。

filterなし:

- `start_date` / `end_date` / `time_range`をbodyへ含めない。
- `null`を送らない。
- 既存固定fieldを変更しない。

adapterはvalid VOをTavily表現へ写像するだけで、日付順序や期間意味を再検証しない。

### 15. 日単位prefilterの限界

Tavilyへ送れる条件は日単位である。`last_24_hours`では取りこぼしを避けるため、
下限instantを含むJST日からas_of JST日翌日までの広いenvelopeを送る。この結果、
24時間より少し古いcandidateが返る可能性は残る。

またTavilyはpublish dateまたはlast updated dateをfilter基準とする一方、responseの
`published_date`は欠損し得る。したがって本sliceの保証は次に限定する。

```text
保証する: Tavily検索をas_of由来の日付範囲で絞る
保証しない: 全candidateが時刻精度で対象期間内と証明される
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

- start境界とend境界について、それぞれ公開日時を別手段で確認できる既知記事を
  最大3件使う。
- 各fixtureはfilterなしで取得可能なことを先に確認し、filterありを最大2回試す。
- `max_results=20`を使い、rankに現れない記事だけで包含・除外を断定しない。
- いずれかのfixtureで境界規則を直接確認できれば`conclusive`とする。
- 全fixture・全試行で判定できなければ`inconclusive`として打ち切り、無期限に再試行しない。

`conclusive`では観測結果に合わせてadapter mappingを確定する。`inconclusive`でも本slice
全体はblockせず、取りこぼし回避を優先して次の保守的mappingを採用する。

```text
provider start_date = internal start_date - 1日
provider end_date   = internal end_date
```

これは公式の`after` / `before`記述に対しstart対象日を確実にenvelopeへ含めるための
design-time fallbackであり、runtime fallbackや設定分岐ではない。providerがstart日を
inclusiveに扱う場合は最大1日古い結果が混ざり得る。比較timezoneを確定できない場合の
JSTとのずれと合わせ、本filterを厳密な後段検証ではなく粗いprovider prefilterとして扱う。
probe結果、採用mapping、`conclusive|inconclusive`を本仕様へ追記して実装確定とする。

## Error contract

| 条件 | 挙動 |
| --- | --- |
| `target_time_window=None` | 意図的な期間なしとしてfilterなし検索 |
| `latest` | 直近7日の絶対日付へ解決して検索 |
| `recent_default` | 直近60日の絶対日付へ解決して検索 |
| 対応済みkind | 絶対日付へ解決して検索 |
| `unsupported_explicit_window` | 全task `time_filter_failed`、status-to-missing写像、metric/log |
| future `calendar_month` | 全task `time_filter_failed`、status-to-missing写像、metric/log |
| 期間validation / resolution failure | `recent_default` / `None`へfallbackしない |
| naive `as_of` | programming errorとして伝播 |
| planner未知kind / invalid month / invalid days | response validation failure → 1回repair → 未回復なら既存internal-only fallback。`time_filter_failed`にはしない |
| resolver未知kind | exhaustiveness違反として伝播 |
| `start_date >= end_date` | VO構築時に拒否し、バグとして伝播 |
| Tavily HTTP / timeout | 既存のprovider query失敗分類 |
| candidate `published_at`欠損 | 既存どおりcandidateを保持 |

## Planner period eval

prompt/schema変更後、実Geminiまたは既存adapter probeで固定質問セットを評価する。

再現条件を次で固定する。

```text
as_of: 2026-07-12T00:30:00Z
product timezone: Asia/Tokyo
model: GEMINI_QUESTION_PLANNER_SPEC.model（現行 gemini-2.5-flash-lite）
prompt/spec version: GEMINI_QUESTION_PLANNER_SPEC.version
trials: 各case 3回
```

モデル名とspec versionは実行時の値を結果へ記録し、仕様中の現行model名と異なる場合は
実行時値を正本とする。semantic accuracyは初回出力だけで採点する。schema違反に対する
1回repairは別集計のstructural recovery rateとし、retry後の値を初回semantic accuracyへ
混ぜない。初回schema違反はprimary semantic accuracyでは不正解として数える。

最低限含める表現:

- 今日、本日、today
- 昨日、昨日公開、yesterday（expected: `yesterday`）
- 直近24時間、ここ24時間、past 24 hours
- 直近1週間、ここ7日、最近7日、last 7 days
- 今週、this week
- 先週公開、last week（expected: `last_week`）
- 過去3日、直近14日、past 60 days（expected: 対応する`last_n_days`）
- 直近1か月、ここ30日、past 30 days
- 今月、this month
- 最新、直近のニュース、latest（expected: `latest`）
- 最近の動向、新しい情報を踏まえて、recent developments等、時間指定はないが
  新しい情報が必要な企業動向・市場反応（expected: `recent_default`）
- 公式仕様・規制原文・過去決算・歴史的背景等、鮮度要求も時間表現もない質問
  （expected: `None`）
- 2026年6月、June 2026
- 2027年の市場予測等、future対象時期だがpublication windowではない質問
- 前四半期に公開、2026年度に公開、任意の開始・終了日、過去90日等、V1語彙外の
  明示publication期間（expected: `unsupported_explicit_window`）
- 先週の出来事を解説、2026年度の業績見通し等、同じ語彙が質問対象時期を表す質問
  （expected: `None`。表現はcollection goal / queryへ保持）

記録する値:

- schema / Pydantic構造違反件数。
- retry回数。
- 初回semantic採点とretry後structural recoveryの別集計。
- expected kindとの一致率。
- future対象時期をpublication filterへ誤変換した件数。
- 時間表現も鮮度意図もない質問へwindowを発明した件数。
- 未対応の明示publication期間を`None`または近似kindへ丸めた件数。

Done gateは結果を見る前に次で固定する。

- eval setは最低32件とし、上記の各分類を複数表現で含める。
- 各caseを3回実行し、case数・総試行数・as_of・timezone・model・spec versionを記録する。
- schema / Pydantic構造違反は1回repair後0件。
- 初回schema valid率と、schema違反caseのrepair成功率を別に記録する。
- 明示的な対応期間のkind正解率は100%。
- 言い換えと曖昧表現を含む全体のsemantic kind accuracyは95%以上。
- future対象時期をpublication filterへ誤変換した件数は0件。
- 時間表現も鮮度意図もない質問は`None` 100%とし、偽陽性windowは0件。
- 鮮度意図はあるが期間幅の指定がない質問は`recent_default`とし、前項の`None` cohortと
  混ぜない。
- 意図的な期間なしケースを`latest` / `recent_default`へ誤変換した件数は0件。
- V1語彙外の明示publication期間は`unsupported_explicit_window` 100%とし、`None`、
  `latest`、`recent_default`、最近傍kindへの丸めは0件。
- production相当または手動で層化したexternal-search対象質問を別に最低200件評価し、
  `unsupported_explicit_window`率を全対象質問の2%以下とする。2%を超えた場合は
  fail-closedのまま出荷せず、頻出表現を解決可能kindへ追加して再評価する。
- usage gateの分母はretrieval modeの正解が`external` / `internal_and_external`である質問とし、
  sanitized済みdatasetのversionまたはhash、母数、unsupported件数を記録する。raw実質問や
  個人情報をdatasetへ保存しない。
- 質問対象時期として使われた語彙外の時間表現をpublication windowへ誤変換した件数は0件。
- 「最新」は`latest`、「新しさ重視だが幅未指定」は`recent_default`として区別する。
- 実測値、retry回数、失敗caseを本仕様へ追記する。

raw response、質問に個人情報を含むsample、API keyは保存・commitしない。

## Test priority and source of truth

実装は各stepでtestを先に追加し、新しい保証についてredを確認してから実装する。

### P0: 本sliceの正しさとfail-closed

1. 型付き`TargetTimeWindow`とGemini schemaの閉集合。
2. JST基準のresolver、昨日・先週・可変N日・60日default・月・年・timezone境界。
3. `ExternalSearchDateFilter` VOの順序保証。
4. request単位resolver 1回と、全task / queryへの同一filter伝搬。
5. future month / unsupported explicit windowのtyped fail-closed、downstream未呼び出し、
   naive `as_of`伝播。
6. 非`None` filter伝搬時のprovider部分失敗・全失敗・timeout分類の回帰防止。
7. Tavily filtered / unfiltered payloadのHTTP contract。
8. external-only / mixed plan別のstatus-to-missing写像、DB永続化、既存API投影。
9. metric / warningのrequest単位1回、閉じた属性、機密値非漏洩、sink失敗非影響。
10. Tavily start/end境界と比較timezoneの有限回実API probe。inconclusive時は保守的mappingを
    採用して無期限にblockしない。

### P1: 意味品質と表示一貫性

1. 全kindのdeterministic表示。
2. query generator / evidence answer promptへ同じ期間意味が渡ること。
3. plan draftからcompleted planへのtyped値保持。
4. planner期間evalのsemantic accuracy、`None` / `latest` / `recent_default`の区別。
5. 対応する昨日・先週・可変N日、未対応の明示publication期間、質問対象時期の区別。
6. production相当質問におけるunsupported率の出荷gate。

### P2: 過剰または脆いため追加しないtest

- 全kind × 全timezone × 全月の直積。
- service / runner / evidence collection / orchestration全層で同じdate値を再assertするtest。
- `datetime.now`未使用をsource scanやmonkeypatchで確認するtest。
- helper共有を関数identityやprivate call順で確認するtest。
- structlogの標準fieldを含むlog dict完全一致。
- 実TavilyのURL・順位・件数をCIでexact assertするtest。
- `model_construct`で不正VOを作りadapterへ渡すtest。

既存testで保証済みの次は、fake signature / fixtureを新contractへ追従させるだけとし、
期間専用の重複testを増やさない。

- EvidenceCollectionの期間 / `as_of`伝搬とexternal例外伝播。
- runnerの並列上限、query cap、provider部分・全失敗、timeout、selector retry、pool rank。
- TavilyのBearer、max results、HTTP/JSON error、candidate mapping、日付parse、unknown日付保持。
- orchestrationのmissing順序・dedupe・missingによる`insufficient`。
- 保存済み`missing_aspects`から公開`missingAspects`へのAPI投影。

## Test design

### Planning contract / schema

1. `yesterday`、`last_week`、`last_n_days`、`recent_default`、
   `unsupported_explicit_window`を含む全kindを受理し、未知kindを拒否する。
2. `calendar_month`だけyear / month、`last_n_days`だけdaysを要求する。
3. year 0 / 10000、month 0 / 13、days 0 / 61、各required field欠損、extra fieldを拒否する。
4. 他kindのyear / month / daysを拒否する。
5. Gemini schemaのenum、year/month/days範囲、extra field拒否がPydantic contractと一致する。
6. planner promptがpublication windowと質問対象時期を分離する。
7. plan draftからcompleted planへtyped windowをそのまま渡す。
8. deterministic表示helperが全kindを網羅する。
9. 未知kindはresponse validation failureとなり、1回repair後も未回復なら既存の
   `InternalRetrievalPlan` fallbackへ入り、resolverや`time_filter_failed`へ到達しない。

### Date filter VO / resolver

1. `start_date < end_date`を受理する。
2. same / reverse dateをVO構築時に拒否する。
3. `None`がfilterなしになる。
4. `today` / `yesterday` / `this_week` / `last_week` / `this_month`をJST暦日で解決し、
   `yesterday`と`last_24_hours`、`last_week`と`this_week`を区別する。
5. `last_24_hours` / `last_7_days` / `last_30_days` / `last_n_days(1|3|60)` /
   `latest` / `recent_default`をinstant起点で
   計算し、JST date envelopeへ投影する。
6. UTCでは前日だがJSTでは当日となるas_of境界を固定する。
7. `as_of=2026-07-31T15:30:00Z`（JST 2026-08-01 00:30）で、`this_month`が
   2026-08-01〜2026-08-02、`calendar_month(2026, 7)`が過去月全範囲、
   `calendar_month(2026, 8)`がfutureではなく当月clipになることを固定する。
8. `calendar_month`の過去月、当月clip、12月、閏年2月を固定する。
9. future monthを`ExternalSearchDateFilterResolutionError`の
   `future_calendar_month`へ分類する。
10. `unsupported_explicit_window`を同errorの同名reasonへ分類する。
11. errorの`str()`、`args`、`repr()`が、class名等の固定表現を除いて固定reason codeだけを
    含み、year / month、質問、sentinel用のprobe文字列を含まないことを固定する。
12. naive as_ofが分類済みerrorではなく伝播する。
13. 未知kindのfallbackが存在しないことをexhaustivenessで固定する。

### Runner / report

1. `ResearchTaskReport`直接構築testで`time_filter_failed`の全field不変条件を固定し、
   いずれか1つでも非既定値なら拒否する。
2. filterあり・`None`・fail-closedの各経路でresolverがrequest単位で1回だけ呼ばれる。
3. 解決済みfilterが全task・全queryへ同値で渡る。
4. filterなしでは全queryへ`None`が渡り、providerの既存payload規則を変えない。
5. query generatorにはdeterministic表示文字列が渡る。
6. `latest`は7日、`recent_default`は60日、`None`はfilterなしとして伝搬する。
7. future monthと`unsupported_explicit_window`では全task分の`time_filter_failed` reportを返す。
8. fail-closedではquery generator / provider / selectorを呼ばない。
9. 各fail-closed reportは閉じたreasonと全field既定値を持ち、表示文言を持たない。
10. `time_filter_failed`以外のreportがreasonを持つことを拒否する。
11. naive `as_of`がreportへ丸められず伝播し、failed metric / warningを作らない。
12. **非`None` filterがproviderへ渡る状態で**部分失敗、全失敗、timeoutの既存分類と
    report集約が回帰しない。既存失敗testをfilter付きparameterへ更新して正本とし、
    同じ分類だけを確認する重複testは作らない。
13. fail-closed時に`queries_generated`、`candidates_fetched`、`evidence_selected` activity
    eventが0件であり、専用eventもemitしない。

### User / persistence visibility

1. external-onlyで全taskが`time_filter_failed`、evidence 0件では一般的な根拠0件を抑制し、
   draft missingを取り込まず、期間失敗文言1件だけにする。
2. internal+externalでinternal evidenceがある場合は期間失敗文言を1件追加し、独立した
   draft missingを保持して`insufficient`にする。
3. internal+externalでinternal evidenceも0件の場合はdraft missingを取り込まず、
   一般的な根拠0件と期間失敗文言の2件を許容する。internal collection failureがあれば
   その固定文言を追加し、期間失敗文言が1回だけであることを固定する。
4. repository integrationでcompleted runの`AgentMessage.missing_aspects`へ期間失敗文言が
   永続化される。
5. thread projection / APIは既存の任意missing投影testを再利用し、固定文言専用の
   route testを追加しない。
6. 公開API shapeを追加せず既存`missingAspects`で見える。

### Operator visibility

1. resolved / not requested / failed metricをrequest単位で1回記録する。
2. `target_time_window=None`ではresolver 1回、`not_requested` metric 1回、
   resolved / failed metric 0回、warning 0回である。
3. fail-closed warningを1回だけ記録する。
4. metric labelとwarning fieldが閉集合である。
5. raw期間、year、month、days、query、質問、URL、API keyを含めない。
6. observability sink失敗を検索失敗へ昇格させない。

metric不在assertionでは、既存`tests/logfire/_metric_helpers.py`の
`collected_metrics(capfire)`を1回だけ読み、metric collectionが0件のときも空listとして
扱う。複数readやzero-metric時のcollector例外へtestを依存させない。

### Tavily adapter

1. valid filterをprobe後に確定したprovider境界へ写像し、ISO `start_date` / `end_date`で送る。
2. filterありpayloadに`time_range`がない。
3. filterなしpayloadは日付3 keyを含まず、既存fieldと一致する。
4. max results、Bearer header、HTTP error、invalid JSON、candidate mapping、
   `published_date` parseの既存testがgreenのまま。

不正順序filterをadapterへ渡すtestは書かない。順序検証はVO testが所有する。

### Real API probe

既存`backend/scripts/probe_tavily_search.py`を拡張するか、日付filter専用probeを追加する。

- 同じqueryをfilterなし / filterありで実行する。
- start / end各境界につき最大3件、filterなしで取得できる既知ニュースを使い、
  filterありは各fixture最大2回とする。
- `max_results=20`でrank未出現を除外証明として扱わない。
- Tavilyがdate-only境界を比較するtimezoneを観測し、確定不能なら`inconclusive`と記録する。
- `published_date`欠損率と指定envelopeから明らかに外れた結果を確認する。
- raw response、API key、記事本文は保存・commitしない。
- 実測日、queryの非機密な要約、包含規則、比較timezoneの結論だけを本仕様へ追記する。
- 上限内で結果が検索rankに現れなければ`inconclusive`で打ち切る。
- `conclusive`では観測mapping、`inconclusive`では`start-1日 / endそのまま`の
  保守的mappingを採用し、どちらでも有限回のprobe後に実装確定へ進める。

## Implementation surface

```text
backend/app/agent/planning/contract.py
backend/app/agent/planning/ai/prompts.py
backend/app/agent/planning/ai/schema_tool.py
backend/app/agent/evidence_collection/contract.py
backend/app/agent/evidence_collection/service.py
backend/app/agent/evidence_collection/external_search/contract.py
backend/app/agent/evidence_collection/external_search/time_filter.py       # new
backend/app/agent/evidence_collection/external_search/metrics.py           # new
backend/app/agent/evidence_collection/external_search/service.py
backend/app/agent/evidence_collection/external_search/runner.py
backend/app/agent/evidence_collection/external_search/tavily.py
backend/app/agent/evidence_collection/external_search/__init__.py
backend/app/agent/evidence_collection/external_search/ai/deepseek.py
backend/app/agent/evidence_collection/external_search/ai/prompts.py
backend/app/agent/answering/orchestration.py
backend/app/agent/answering/evidence_answer/contract.py
backend/app/agent/answering/evidence_answer/flow.py
backend/app/agent/answering/evidence_answer/ai/gemini.py
backend/app/agent/answering/evidence_answer/ai/prompt.py

backend/tests/agent/planning/test_contract.py
backend/tests/agent/planning/test_planner.py
backend/tests/agent/planning/ai/test_gemini_question_planner.py
backend/tests/agent/planning/ai/test_question_planner_prompt_schema.py
backend/tests/agent/evidence_collection/test_evidence_collection.py
backend/tests/agent/evidence_collection/external_search/test_contract.py    # new
backend/tests/agent/evidence_collection/external_search/test_time_filter.py # new
backend/tests/agent/evidence_collection/external_search/test_service.py
backend/tests/agent/evidence_collection/external_search/test_research_runner.py
backend/tests/agent/evidence_collection/external_search/test_tavily.py
backend/tests/agent/evidence_collection/external_search/ai/test_prompts.py
backend/tests/agent/evidence_collection/external_search/ai/test_deepseek_query_generator.py
backend/tests/agent/answering/test_orchestration.py
backend/tests/agent/answering/evidence_answer/test_flow.py
backend/tests/agent/answering/evidence_answer/ai/test_gemini.py
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
  target_time_window={kind: "last_24_hours", year: null, month: null, days: null}

input.as_of=2026-07-12T00:30:00Z
  -> request-level resolver（1回）
       product timezone=Asia/Tokyo
       start_date=2026-07-11
       end_date=2026-07-13
  -> task/query fan-out
  -> 全Tavily callへ同じdate_filter
  -> existing pool / selector / evidence path

planner output:
  target_time_window=None
  -> plannerがpublication期間なしを意図的に選択
  -> resolver 1回、returns None
  -> not_requested metric 1回、resolved / failed metric 0回、warning 0回
  -> Tavily body has no date keys

planner output:
  target_time_window={kind: "yesterday", year: null, month: null, days: null}
  -> JST前日のdate filter

planner output:
  target_time_window={kind: "last_n_days", year: null, month: null, days: 3}
  -> 直前3日を覆うdate filter

planner output:
  target_time_window={kind: "latest", year: null, month: null, days: null}
  -> 直近7日のdate filter

planner output:
  target_time_window={kind: "recent_default", year: null, month: null, days: null}
  -> 直近60日のdate filter

planner response validation failure（未知kind / invalid month等）
  -> 1回repair
  -> 未回復なら既存InternalRetrievalPlan fallback
  -> resolver / time_filter_failedへ到達しない

planner output:
  target_time_window={kind: "unsupported_explicit_window", year: null, month: null, days: null}
  -> unsupported_explicit_window
  -> None / latest / recent_default / 近似kindへ置換しない
  -> 分類済みfail-closed

planner output:
  target_time_window={kind: "calendar_month", year: 2027, month: 1, days: null}
input.as_of=2026-07-12T00:30:00Z
  -> future_calendar_month
  -> metric / warningを1回
  -> 全task status=time_filter_failed
  -> 全report.missing=[]、reason=future_calendar_month
  -> external-onlyではorchestrationがstatusを期間失敗文言1件へ写像し、
     一般的な根拠0件とdraft missingの取込みを抑制
  -> userのmissingAspectsへ「指定された公開期間を外部検索へ適用できませんでした」を
     1件表示・DB保存
  -> query generator/provider/selectorは未呼び出し
  -> external activity eventは0件

input.as_of=naive datetime
  -> programming errorとして伝播
```

## Open verification items

1. Tavilyのstart日当日・end日当日の包含規則。
2. Tavilyがdate-only値を評価するtimezone。
3. conclusive時に`ExternalSearchDateFilter`をそのまま送るか、adapter境界の補正が必要か。
4. recent newsと期間なし一次情報を同一runで扱う後続のtask単位期間policy。

Tavily境界は有限回probeし、conclusiveなら実測mapping、inconclusiveなら
`start-1日 / endそのまま`を採用する。結果と採用mappingを本仕様へ追記して
実装完了扱いにする。
