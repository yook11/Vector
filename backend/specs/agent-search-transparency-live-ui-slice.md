# Agent search transparency live UI slice 仕様

Status: Draft — 2026-07-14

## 1. Summary

本sliceは、Research runの外部検索中に、実際の検索queryと検索結果から得たsource hostを
ユーザーへ短時間で切り替えて表示し、調査対象と情報源の広がりを伝える。

表示例:

```text
情報収集中
“NVIDIA Rubin release schedule 2026” など3件を検索中
14サイト・検索結果20件を確認中
reuters.com
```

最下段のsource hostだけを一定間隔で切り替える。件数表示は固定し、表示中のsource hostが
変わってもlayoutを動かさない。

本sliceでいう「URL表示」は、raw URLやclickable linkの表示ではない。安全性を優先し、
検証済み候補URLから投影したhostnameだけをplain textで表示する。path、query、fragment、
userinfo、port、scheme、記事title、snippetはlive eventへ入れない。

## 2. Problem

外部検索では、生成済みquery、候補件数、選別済み根拠件数をactivityとして通知している。
frontendも先頭queryと件数を表示できるが、次のactivityで最新表示が置き換わるため、
何を検索し、どの程度広い情報源を確認しているかが伝わりにくい。

検索providerは複数サイトの検索結果URLを返しているが、現在の
`external_search.candidates_fetched`は`candidateCount`しか公開しない。ユーザーは
「検索結果12件」という数だけを見ても、1サイトを繰り返し見ているのか、複数サイトを横断しているのか
判断できない。

一方、raw URLをそのままlive payloadへ追加すると、次の危険が増える。

- userinfo、path、query parameter、fragmentに含まれる不要な情報の露出
- `javascript:`等の不正scheme、private IP、特殊用途host、制御文字の混入
- Unicode homographやbidi制御文字による見た目の偽装
- SSE frame、HTML、attribute、URL contextへの注入
- 長大なURLや大量のhostによるpayload増大とDOM更新負荷
- sourceをlog / metric labelへ載せることによる情報漏えいと高cardinality
- live表示をclickableにした場合の別origin遷移、opener、trackingの追加リスク

したがって、検索の透明性は高めるが、raw URLをlive配信する契約は作らない。

## 3. Evidence

2026-07-14時点の実装を根拠とする。

### 3.1 External search domain

- `backend/app/agent/evidence_collection/external_search/contract.py`
  - 1 taskあたりqueryは最大3件、queryは最大200文字である。
  - 1 taskあたり、検証・重複排除・上限適用後の検索結果集合は最大20件である。
  - 現在の`ExternalSearchCandidate`は`url: SafeUrl`と`source_name`を持つが、本sliceで
    `ExternalSearchResult`へ改名する。
- `backend/app/agent/evidence_collection/external_search/tavily.py`
  - provider responseのURLを`SafeUrl`で検証してから現在の`ExternalSearchCandidate`へ変換する。
    本slice後は同じ境界型を`ExternalSearchResult`と呼ぶ。
  - 現在の`source_name`はURLのhostnameを取り出し、先頭`www.`を除いて作る。
  - Tavily requestは特定domainを事前指定せず、news topicのWeb検索を行う。
- `backend/app/agent/evidence_collection/external_search/runner.py`
  - query生成後に`external_search.queries_generated`を通知する。
  - provider検索を終え、task内の検証済み検索結果集合を作った後に、現在は
    `external_search.candidates_fetched`を通知する。本sliceで
    `external_search.results_collected`へ置き換える。
  - 複数taskは並列であり、task間のactivity順序は非決定的である。

### 3.2 Existing event contract

- `backend/app/agent/contract.py`
  - `ExternalSearchQueriesGeneratedEvent`は`task_index`と`queries`を持つ。
  - 現在の`ExternalSearchCandidatesFetchedEvent`は`task_index`と`candidate_count`だけを持つ。
    本sliceで`ExternalSearchResultsCollectedEvent`、`result_count`へ改名する。
- `backend/app/schemas/research.py`
  - pollingの`recentEvents`は同じactivity語彙をcamelCaseで公開する。
- `backend/app/agent/live_updates/reporters.py`
  - activityは既存Redis ListとRedis Streamへbest-effortでfan-outされる。
- `backend/app/agent/live_updates/sse.py`
  - activityをnested `activity` fieldへ入れ、JavaScript境界だけcamelCaseへ変換する。
  - dataは1行JSONとしてserializeされる。

### 3.3 Existing frontend

- `frontend/src/features/research/live/events.ts`
  - SSEとpolling activityをruntime validationする。
  - 既知activity以外は表示stateへ渡さない。
- `frontend/src/features/research/components/ActiveRunStatus.tsx`
  - queryが1件なら全文、複数なら先頭queryと件数を表示する。
  - 現在のcandidates fetchedは検索結果件数だけを表示する。
- `frontend/src/features/research/live/reducer.ts`
  - activity履歴は持たず、現在の最新activityだけを保持する。

### 3.4 URL boundary

- `backend/app/shared/security/safe_url.py`
  - `SafeUrl`はHTTP / HTTPS、構文、長さ、IP literalのpublic性を検証する。
  - DNS名の解決やcanonical化は行わず、元文字列を保持する。
  - したがって`SafeUrl`であることだけを、そのまま表示用hostnameの契約にはしない。

## 4. Terminology

- **search result**: search provider responseから構造検証を通過した個々の検索結果。
- **result pool**: task内でURL重複排除と上限適用を終えた検索結果集合。
- **source host**: search result URLのhostname部分。
- **display-safe source host**: 本仕様の表示用projectionを通過したASCII hostname。
- **sourceHostCount**: result poolから得たdisplay-safe source hostの重複排除後総数。
- **sourceHosts**: sourceHostCountのうち、provider rankに基づく先頭最大12件。
- **rotation**: 同じresults collected activity内のsourceHostsをfrontendで順番に表示すること。

`sourceHostCount`は検索結果件数ではない。同一hostから3件のresultがあれば、
`resultCount = 3`、`sourceHostCount = 1`となる。

`ExternalSearchResult`は検索providerの生responseではない。URL・title等の構造検証を通過した
検索結果である。selectorが採用し、claimと選定理由を加えた後は`ExternalSearchEvidence`になる。

内部語彙は次へ同期する。

| Before | After |
|---|---|
| `ExternalSearchCandidate` | `ExternalSearchResult` |
| `CANDIDATE_SNIPPET_MAX_CHARS` | `SEARCH_RESULT_SNIPPET_MAX_CHARS` |
| `EXTERNAL_SEARCH_CANDIDATES_PER_QUERY` | `EXTERNAL_SEARCH_RESULTS_PER_QUERY` |
| `EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK` | `EXTERNAL_SEARCH_RESULT_POOL_LIMIT_PER_TASK` |
| `EvidenceSelection.candidate_index` | `EvidenceSelection.result_index` |
| `ResearchTaskReport.candidate_count` | `ResearchTaskReport.result_count` |
| `candidates` / `candidate_pool` | `results` / `result_pool` |
| selector promptの`candidates` block | `results` block |

`ExternalSearchRunResult`はrunner全体の戻り値を表す既存aggregateであり、個々の
`ExternalSearchResult`とは責務が異なるため、本sliceでは維持する。

## 5. Scope

本sliceは次を実装対象とする。

1. 内部型`ExternalSearchCandidate`を`ExternalSearchResult`へ改名し、同じ概念へ`Candidate`と
   `Result`の2語を使わない。
2. `EvidenceSelection.candidate_index`、candidate関連定数・変数・report fieldもresult語彙へ同期する。
3. result poolのSafeUrlからdisplay-safe source hostを投影する。
4. `external_search.candidates_fetched`を`external_search.results_collected`へ置き換え、
   `resultCount`、`sourceHostCount`、`sourceHosts`を公開する。
5. Redis List、Redis Stream、SSE、polling `recentEvents`の両経路で同じ値を公開する。
6. frontend runtime parserで新eventとfieldを検証する。
7. 固定した件数表示とsource host rotationをResearch live UIへ追加する。
8. hidden、reduced motion、activity切替、terminal、unmount時のtimer lifecycleを定義する。
9. URL由来値に対するsecurity、privacy、protocol integrity testを追加する。

## 6. Invariants

### 6.1 Truthfulness

1. 画面へ表示するsource hostは、そのrun / attemptで実際にproviderから取得し、result poolへ残った
   SafeUrlだけを根拠とする。
2. 架空のsource host、固定demo値、検索前に推測したsite名を表示しない。
3. 特定domainを事前指定していない現在の検索を「reuters.comを検索中」と表現しない。
4. result収集後は「確認中」「検索結果を確認」と表現し、事前の検索対象と誤認させない。
5. `resultCount`はresult pool件数、`sourceHostCount`は表示可能なunique host総数として混同しない。
6. `sourceHosts`の順序はresult poolのprovider rankにおける最初の出現順とし、表示映えのために
   並べ替えたり水増ししたりしない。
6a. 構造検証済みの個々の検索結果は、provider adapter、runner、selector prompt、selection index、reportを
    通して`ExternalSearchResult` / result語彙で統一する。
6b. `ExternalSearchEvidence`はselectorが採用した検索結果だけを表す。検索結果であるだけの値をevidenceと
    呼ばず、採用済みevidenceをresultへ戻さない。
6c. 初回検索と将来の追加検索は検索結果の状態とは別軸である。追加検索を実装するときはsearch round / triggerで
    表現し、`Candidate`語彙を検索回数の区別へ流用しない。

### 6.2 URL minimization

7. live activityへraw URLを入れない。
8. scheme、userinfo、password、port、path、query、fragmentをlive activityへ入れない。
9. 記事title、snippet、raw provider responseをlive activityへ入れない。
10. 表示用projectionの入力は`ExternalSearchResult.url: SafeUrl`だけとし、providerのraw stringや
    任意の`source_name`を信頼しない。
11. frontendへ公開するfield名は、値がhostnameであることを表す`sourceHosts`と
    `sourceHostCount`に固定する。`urls`、`links`、`sources`等の曖昧な名前を使わない。
12. source hostはclickable linkにせず、plain React textとしてだけ描画する。
13. source host表示を理由にbrowserから対象hostへfetch、prefetch、DNS prefetchを行わない。

### 6.3 Display-safe source host

14. projectionはURL parserが返すhostnameだけを入力にする。
15. hostnameは末尾dotを除き、小文字化し、先頭の正確な`www.`を1回だけ除く。
16. Internationalized Domain NameはUnicode表示せず、IDNA ASCII / punycodeへ正規化する。
17. 正規化後はASCII domain label規則、label最大63文字、hostname最大253文字を検証する。
18. 空host、single-label host、IP literal、localhost、特殊用途・非公開用途suffix、制御文字、空白、
    slash、backslash、colon、at sign、query / fragment delimiterを含む値を表示対象から除外する。
19. 少なくとも`.localhost`、`.local`、`.internal`、`.invalid`、`.example`、`.test`は除外する。
20. IP literalはSafeUrlがpublic IPとして受理していてもsource host表示から除外する。
21. bidi制御文字やUnicode confusableをUnicodeのまま表示しない。IDNA変換できない値は除外する。
22. projection失敗はsearch result自体や回答生成を失敗させない。そのhostだけを表示対象から除外する。
23. projectionのためにDNS resolveや外部network accessを追加しない。

### 6.4 Cardinality and payload

24. `sourceHosts`は重複なし、最大12件とする。
25. `sourceHostCount`はcap適用前のdisplay-safe unique host総数とする。
26. `0 <= len(sourceHosts) <= sourceHostCount <= resultCount`を保証する。
27. `sourceHostCount = 0`なら`sourceHosts = []`とする。
28. 12件を超えるhostはpayloadへ入れず、総数だけ`sourceHostCount`へ残す。
29. queryは既存上限を維持し、1 taskあたり最大3件、1件最大200文字とする。
30. source host、query、run ID、user IDをlog本文、metric label、exception messageへ入れない。

### 6.5 Delivery and compatibility

31. activityは既存どおりRedis ListとRedis Streamへ独立してbest-effort publishする。
32. source host projectionやpublishの失敗で検索、回答生成、DB確定結果を失敗させない。
33. SSEではnested `activity`とcamelCase fieldを維持する。
34. BFFはSSE bodyをbyte passthroughし、source hostを再解釈しない。
35. polling `recentEvents`とSSEでfield名、意味、上限を一致させる。
36. canonicalな公開eventは`external_search.results_collected`だけとし、新しいproducerは
    `external_search.candidates_fetched`を書かない。
37. Redis TTL内に残る旧`external_search.candidates_fetched`はstorage decode境界だけで受理し、
    `external_search.results_collected`、`resultCount`、host count 0 / emptyへ正規化する。
38. legacy event typeと`candidateCount`をOpenAPI、SSE、frontend stateへ再公開しない。
39. 新eventのsource fieldが欠損する場合は`sourceHostCount = 0`、`sourceHosts = []`へ正規化する。
40. fieldが存在するが型、上限、canonical host規則、count関係に違反する場合、そのactivityだけを捨て、
    SSE接続と他のlive stateを壊さない。
41. deployはlegacy storage decoderを持つreader / API、frontend、producerの順とする。

### 6.6 UI behavior

42. query activityは現在どおり、1件ならquery、複数なら先頭queryと総数を表示する。
43. results collected activityは`sourceHostCount > 0`なら
    `Nサイト・検索結果M件を確認中`を固定表示する。
44. `sourceHostCount = 0`なら`検索結果M件を確認中`だけを表示し、安全でないhostを代替表示しない。
45. `sourceHosts`が1件ならrotation timerを作らず、そのhostを固定表示する。
46. `sourceHosts`が2件以上なら先頭hostを即時表示し、800msごとに次のhostへ切り替える。
47. 最後のhostの次は先頭へ戻る。activityが同じ間だけ循環する。
48. host表示領域は固定高とし、長いhostnameや切替でstage、query、message layoutを動かさない。
49. hostは1行、省略表示可能とするが、HTML titleやtooltipへraw URLを入れない。
50. 新しいactivity、attempt、run、thread、terminal、polling-only suppression、component unmountで
    旧rotation timerを必ず停止する。
51. 新しいresults collected activityを受けたらindexを0へ戻し、新しいsourceHostsだけを表示する。
52. background tabではrotation timerを停止する。visible復帰時は現在のactivityで1つだけtimerを再開する。
53. `prefers-reduced-motion: reduce`ではrotationしない。先頭hostと
    `ほかNサイト`の静的表示へ劣化する。
54. 複数taskのactivity順序は非決定的であり、最新の有効なStream IDを持つactivityを表示する。
    genericなactivity履歴や無制限配列を復活させない。

### 6.7 Accessibility

55. 800msごとのhost切替を`aria-live`で読み上げない。
56. 視覚的なrotating hostはscreen readerの連続通知対象から外す。
57. screen readerにはresults collected activity受理時だけ
    `Nサイトから検索結果M件を確認しています`相当の安定した要約を最大1回通知する。
58. query、host、result countの細かな切替でfocusを移動しない。
59. reduced motion時も情報量を失わず、sourceHostCountと検索結果件数を静的に伝える。

### 6.8 Security and privacy

60. frontendはsource hostを`dangerouslySetInnerHTML`、HTML attribute、CSS、URL、script contextへ渡さない。
61. source hostに似た文字列をanchorへ変換するauto-link処理を行わない。
62. SSE serializerは既存の1行JSON escapeを維持し、CR / LF等をframe区切りとして解釈させない。
63. source hostはrun所有者だけが読める既存SSE / polling認可境界を迂回しない。
64. validation failureの観測は固定reasonと件数だけに限定し、拒否した値を記録しない。
65. UI表示用source hostを後続のserver fetch、redirect、allowlist判断、認可判断へ再利用しない。

## 7. Non-goals

- raw URLまたは記事pathのlive表示
- source hostのclickable link化
- final answer source link契約の変更
- browserから候補siteへのprefetch / fetch
- search provider名やAPI endpointの表示
- 記事title、snippet、本文、provider scoreの表示
- plannerのreason、prompt、chain-of-thoughtの表示
- queryやsiteの永続履歴、activity timeline、監査画面
- 外部検索の対象domain指定や検索algorithmの変更
- internal search query本文の新規公開
- `collectionGoal`、`targetTimeWindow`、部分失敗、retry理由の新規event追加
- 将来の追加検索を表す`searchRound` / `trigger`の先行追加
- activityのglobal ordering保証
- source host単位のclick analytics
- URL canonicalization基盤全体の再設計
- public suffix list用の新規dependency追加

`collectionGoal`、対象期間、部分失敗、回答再生成理由は有用なfollow-up候補だが、本sliceのDoneには
含めない。

## 8. Done

次をすべて満たしたら本sliceを完了とする。

- `external_search.queries_generated`の既存query表示が維持される。
- 内部の検証済み検索結果型が`ExternalSearchResult`へ統一され、`ExternalSearchCandidate`が残らない。
- selector参照field、定数、report、変数もresult語彙へ揃い、同じ概念をcandidateとresultで呼び分けない。
- canonical public eventが`external_search.results_collected`となり、resultCount、sourceHostCount、
  最大12件のsourceHostsを公開する。
- 旧`external_search.candidates_fetched`はstorage decode時だけ新eventへ正規化され、公開境界へ残らない。
- sourceHostsはSafeUrlからdisplay用projectionしたhostnameだけで、raw URLの他要素を含まない。
- SSEとpolling recentEventsが新旧shapeを安全に扱う。
- frontendが件数を固定表示し、source hostを800ms間隔で切り替える。
- reduced motion、background、activity切替、terminal、unmountでtimerが正しく停止・劣化する。
- rotating hostをscreen readerへ連続通知しない。
- malicious URL、IDN、IP literal、control character、長大入力、重複、大量hostの必須testが通る。
- raw URL、query、source hostをlog / metricへ追加していない。
- backend Pydantic schema、OpenAPI、generated TypeScript types、frontend runtime parserが同期している。
- 既存の検索、回答生成、Redis障害時polling fallback、final DB answerを回帰させない。

## 9. Contract design

### 9.1 Domain event

内部型と公開eventを、次の状態遷移が読める語彙へ揃える。

```text
provider raw response
  -> ExternalSearchResult
  -> EvidenceSelection
  -> ExternalSearchEvidence
  -> AnswerEvidenceItem
  -> final cited AnswerSource
```

検証済み検索結果のdomain modelは次とする。

```python
class ExternalSearchResult(BaseModel):
    url: SafeUrl
    title: str
    snippet: str | None
    published_at: datetime | None
    source_name: str | None
```

`ExternalSearchResult`は構造検証済みだが、根拠として採用済みではない。selectorが採用し、claimと
選定理由を加えたものだけが`ExternalSearchEvidence`になる。さらに最終回答の`AnswerSource`は、
回答本文が実際にcitationしたevidenceだけから作る。

公開activityは次とする。

```python
class ExternalSearchResultsCollectedEvent(BaseModel):
    type: Literal["external_search.results_collected"]
    task_index: int
    result_count: int
    source_host_count: int = 0
    source_hosts: list[DisplaySafeSourceHost] = Field(default_factory=list)
```

`DisplaySafeSourceHost`は表示用途のASCII hostnameであり、fetch安全性やdomain ownershipを保証する型ではない。
この値をURLへ戻してnetwork accessに使ってはならない。

### 9.2 Public API / polling shape

FastAPI Pydantic schemaをAPIのSSoTとし、camelCaseでは次を公開する。

```json
{
  "type": "external_search.results_collected",
  "ts": "2026-07-14T00:00:00Z",
  "taskIndex": 0,
  "resultCount": 20,
  "sourceHostCount": 14,
  "sourceHosts": [
    "reuters.com",
    "nvidia.com",
    "techcrunch.com"
  ]
}
```

`sourceHosts`は完全な14件を表すとは限らない。配列は最大12件であり、全体数は
`sourceHostCount`を正本とする。

### 9.3 SSE shape

既存のnested activity契約を維持する。

```text
event: activity
data: {"attemptEpoch":2,"activity":{"type":"external_search.results_collected","taskIndex":0,"resultCount":20,"sourceHostCount":14,"sourceHosts":["reuters.com","nvidia.com","techcrunch.com"]}}
```

SSE dataへ`url`、`urls`、`href`、`path`、`query`、`fragment`を追加しない。

### 9.4 Host projection algorithm

result poolをprovider rank順に走査する。

1. search resultの`SafeUrl.root`を標準URL parserへ渡し、hostnameを得る。
2. hostnameがなければ除外する。
3. trailing dotを除きlowercase化する。
4. IDNA ASCIIへ変換する。失敗したら除外する。
5. 先頭の`www.`を1回だけ除く。
6. IP literal、single-label、特殊用途suffix、domain label違反、長さ違反を除外する。
7. canonical host単位で重複排除する。
8. unique総数を`sourceHostCount`とする。
9. 先頭最大12件を`sourceHosts`とする。

projectionはpure functionとし、DNS、HTTP、database、Redisへアクセスしない。

### 9.5 Legacy event migration

public activity typeとfield名の変更は破壊的変更なので、Redis TTL内の旧entryを直接新schemaで
rejectしてはならない。

storage decode境界に、公開API schemaとは分離したlegacy decoderを置く。

```text
legacy storage event
  type = external_search.candidates_fetched
  candidate_count = 20

decode / normalize
  type = external_search.results_collected
  result_count = 20
  source_host_count = 0
  source_hosts = []
```

legacy class / discriminatorはRedis List / Stream decodeにだけ存在してよい。FastAPI response schema、
OpenAPI、SSE event、generated TypeScript type、frontend stateへは新語彙だけを公開する。

移行順は次に固定する。

1. backend reader / APIへlegacy decodeとcanonical normalizationをdeployする。
2. frontendへ`external_search.results_collected` parserと表示をdeployする。
3. worker producerを新eventへ切り替える。
4. 旧entry TTL 15分と既存接続の終了を待つ。
5. legacy storage decoderの削除は別follow-upで判断する。

旧frontendが一時的に新activityを未知として捨てても、stage、回答delta、terminal、polling、最終DB結果は
維持される。検索結果の詳細表示が一時的に欠けるだけで、runの正しさへ影響させない。

## 10. Presentation design

### 10.1 Query phase

```text
情報収集中
“NVIDIA Rubin release schedule 2026” など3件を検索中
```

queryはLLM生成物であるためplain textとして描画し、最大2行に収める。queryの切替アニメーションは
本sliceで追加しない。

### 10.2 Results collected phase

```text
情報収集中
14サイト・検索結果20件を確認中
reuters.com
```

`reuters.com`の行だけが800msごとに切り替わる。

### 10.3 No display-safe hosts

```text
情報収集中
検索結果20件を確認中
```

危険なhostを表示するより、host行そのものを出さない。

### 10.4 Evidence selection

既存表示を維持する。

```text
情報収集中
根拠3件を選別
```

このactivityへ切り替わった時点でsource host rotationは停止する。

## 11. Failure and degradation

| 条件 | 挙動 |
|---|---|
| URLがSafeUrl validationに失敗 | ExternalSearchResult化せず既存処理に従う |
| display host projectionだけ失敗 | search resultは維持し、host表示からだけ除外 |
| 全hostが除外 | resultCountだけ表示 |
| 旧candidates_fetched entry | storage decoderでresults_collected / resultCountへ正規化 |
| 新eventのsource field欠損 | count 0 / emptyへ正規化 |
| source field schema違反 | event-local invalidとしてactivityだけ捨てる |
| Redis publish失敗 | runを失敗させずpolling / final DB結果へ劣化 |
| SSE接続不能 | polling recentEventsの同shapeを使用 |
| background tab | rotation停止、polling lifecycleは既存契約を維持 |
| reduced motion | 静的要約へ劣化 |
| source hostが12件超 | 12件だけrotationし、総数はsourceHostCountで表示 |

## 12. Expected file changes

### Backend

- `backend/app/agent/contract.py`
  - `ExternalSearchCandidate`から`ExternalSearchResult`への改名。
  - `ExternalSearchResultsCollectedEvent`と追加fieldの不変条件。
- `backend/app/agent/evidence_collection/external_search/runner.py`
  - result poolからhost summaryを作りeventへ渡す。
- `backend/app/agent/evidence_collection/external_search/contract.py`
  - result model、selector index、定数、report fieldの語彙同期。
- `backend/app/agent/evidence_collection/external_search/__init__.py`
  - export名をresult語彙へ同期する。
- `backend/app/agent/evidence_collection/external_search/tavily.py`
  - raw provider itemから`ExternalSearchResult`を作るadapter語彙。
- external evidence selector adapter / schema / prompt
  - `candidate_index`を`result_index`へ同期し、prompt内のcandidate語彙を残さない。
- `backend/app/agent/live_updates/stream.py`と`recent_events.py`
  - 旧storage eventのdecodeとcanonical eventへのnormalization。
- external search境界内の小さなpure projection helper
  - display-safe hostの正規化、除外、dedupe、cap。
- `backend/app/schemas/research.py`
  - polling / OpenAPI shapeの追加field。
- 既存unit / integration test
  - runner、recent events、stream、SSE、router response。

### Frontend

- generated API types
  - backend schema更新後に`/gen-types`を使用する。
- `frontend/src/features/research/live/events.ts`
  - source fieldのruntime validationとlegacy normalization。
- `frontend/src/features/research/components/ActiveRunStatus.tsx`
  - stable count、rotation、reduced motion presentation。
- 必要ならhost rotationを所有する小さなhook / component
  - timerとvisibility lifecycleだけを所有し、EventSourceやpollingを所有しない。
- 既存frontend tests
  - parser、component、timer、accessibility、polling fallback。

### Unchanged

- BFF routeのbyte passthrough
- Redis key、TTL、MAXLEN
- SSE public event name
- answer delta / reset / terminal契約
- final answer source link
- DB schema
- authentication / authorization

## 13. Tests

### 13.1 Host projection unit tests

1. `https://www.Reuters.com/world?a=1#x`から`reuters.com`だけを得る。
2. scheme、port、userinfo、path、query、fragmentが出力へ残らない。
3. trailing dotとuppercaseをcanonical化する。
4. IDNをUnicode表示せずIDNA ASCIIへ変換する。
5. IDNA変換不能、bidi、control character、空白を除外する。
6. IPv4、IPv6、localhost、single-label、特殊用途suffixを除外する。
7. 同一hostの複数URLを1件へdedupeする。
8. provider rankの最初の出現順を維持する。
9. 13件以上ではsourceHostsを12件にcapし、sourceHostCountは総数を維持する。
10. 全host除外でcount 0 / emptyになる。
11. projection failureでresult poolや検索結果を変更しない。
12. pure functionがDNS / HTTPを呼ばない。

### 13.2 Domain / producer tests

1. `ExternalSearchResult`が検証済み検索結果を表し、旧`ExternalSearchCandidate`参照が残らない。
2. `result_index`がresult poolを参照し、選択後に`ExternalSearchEvidence`へ変換される。
3. results collected eventがresultCount、sourceHostCount、sourceHostsを保持する。
4. `len(sourceHosts) <= sourceHostCount <= resultCount`違反をrejectする。
5. runnerが実result poolだけからhost summaryを作る。
6. queryごとの重複resultをpoolでdedupeした後のhostを使う。
7. 複数taskが各task固有のhost summaryを通知する。
8. host projection失敗がsearch taskをfailedにしない。
9. event publish失敗がsearch resultとanswer生成を妨げない。
10. selector schema / prompt / adapterが`result_index`で一致し、旧`candidate_index`を要求しない。
11. `ExternalSearchRunResult` aggregateの意味と戻り値を変更しない。

### 13.3 Redis List / Stream tests

1. 新shapeがListとStreamの両方へfan-outされる。
2. 一方のsink失敗でも他方を試行する。
3. 旧candidates_fetched entryをstorage decode境界でcanonical results_collectedへ変換できる。
4. nested activityのdomain形はsnake_caseを維持する。
5. Redis entryへraw URL、path、query、fragmentが入らない。
6. TTL、MAXLEN、epoch fencingを回帰させない。
7. 新producerが旧candidates_fetched eventを書かない。

### 13.4 SSE / API tests

1. SSEはnested activityにcamelCase source fieldsを出す。
2. polling recentEventsも同じcamelCase shapeを出す。
3. dataは1行JSONで、CR / LFをframeとして注入できない。
4. sourceHostsにraw URL fieldが存在しない。
5. malformed host、過大array、不整合countをfrontendへ正常eventとして渡さない。
6. 旧shapeを15分TTL移行中も安全に扱う。
7. 所有者以外は既存どおり404となり、Redisを読まない。
8. OpenAPIとgenerated TypeScript typesに旧Candidate event / candidateCountが残らない。

### 13.5 Frontend parser tests

1. sourceHostCountとsourceHostsの正常shapeを受理する。
2. 新eventでsource field欠損をlegacy zero / emptyへ正規化する。
3. 負数、小数、MAX_SAFE_INTEGER超過、不整合countをrejectする。
4. 12件超、重複、canonicalでないhost、URL文字列、IP literal、control characterをrejectする。
5. malformed activityだけを捨て、接続を維持する。
6. polling recentEventsとSSEへ同じvalidationを使用する。
7. frontend runtime unionに旧candidates_fetched / candidateCountを残さない。

### 13.6 Presentation / timer tests

1. 14 host / 20 resultsで固定件数と先頭hostを即時表示する。
2. 800msごとにhostが順番に切り替わり、一巡後に先頭へ戻る。
3. 1 hostではtimerを作らない。
4. 0 hostではhost行を表示しない。
5. 新activity、task、attempt、run、thread、terminal、unmountで旧timerを回収する。
6. hidden中は切替せず、visible復帰時にtimerを1つだけ再開する。
7. reduced motionでは切替せず静的要約を表示する。
8. React StrictModeでも同時rotation timerが1つを超えない。
9. 長いhostや切替でlayout heightが変わらない。
10. hostはplain textとしてescapeされ、linkやHTMLにならない。
11. rotating hostをaria-liveで逐次読み上げない。
12. screen reader向け要約をactivity受理時だけ1回通知する。
13. live / reconnectingでは遅いpolling activityで表示を巻き戻さない。
14. polling-onlyでは検証済みrecentEventsから同じ表示を再構築する。

### 13.7 End-to-end contract test

fixture search result URL群から、runner、Redis Stream、SSE serializer、frontend parser、presentationまでを通し、
次を確認する。

- query表示後にresults collected source host表示へ移る。
- 件数とhost順序が一致する。
- raw URLのpath / query / fragment / credentialがどの公開payloadやDOMにも存在しない。
- malicious URL fixtureが回答生成を失敗させず、画面にも現れない。
- terminal後はrotationが止まり、最終DB回答へ置き換わる。

外部Tavilyへ実通信するtestは必須にしない。provider response fixtureで契約を固定する。

## 14. Implementation order

1. display-safe host projectionのunit testとpure functionを作る。
2. domain eventとrunner producerを新fieldへ対応する。
3. Pydantic API schema、recent events、SSE projection testを更新する。
4. `/gen-types`でfrontend generated typesを同期する。
5. frontend parserの新旧shape testを追加する。
6. source host presentationとtimer lifecycle testを追加する。
7. component実装とreduced motion / accessibilityを接続する。
8. fake end-to-end contract testを通す。
9. reader / API / frontendを先にdeployし、producerを最後に有効化する。
10. 実runでquery、host、result、evidence、terminalの順とpayload非露出を確認する。

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| raw URL情報の露出 | hostnameだけを専用projectionし、raw URL fieldを契約へ入れない |
| hostnameによる偽装 | lowercase IDNA ASCII、plain text、no link、canonical validation |
| local / IP表示 | single-label、special-use、IP literalを除外 |
| SSE / DOM injection | 1行JSON、runtime parser、React text、control character拒否 |
| 多量payload | 最大12 host、hostname最大253文字 |
| 高cardinality observability | payload値をlog / metric labelへ入れない |
| 頻繁な更新で読みにくい | 固定件数、host行だけ800ms、固定高 |
| screen readerが騒がしい | rotationは非live、安定要約だけ通知 |
| motion sensitivity | reduced motionで静的表示 |
| timer leak | activity / run / visibility / unmount cleanup test |
| rolling deployで旧entry decode失敗 | default field、新旧parser、reader-first deploy |
| 「検索中」の誤表示 | result収集後は「確認中」と表現 |

## 16. Decision log

- queryは既存eventを再利用し、新しいquery eventを作らない。
- source breadthは「など」で省略するだけでなく、実hostを短時間で切り替えて見せる。
- 検証済み検索結果の内部型は`ExternalSearchResult`、選別後は`ExternalSearchEvidence`とする。
- 公開eventは`ExternalSearchResultsCollectedEvent`、typeは
  `external_search.results_collected`、件数は`resultCount`とする。
- 旧Candidate語彙はstorage migration decoder以外から除去する。
- 初回検索と将来の追加検索の区別はsearch result / evidenceの語彙へ混ぜず、追加検索を実装するsliceで
  search round / triggerとして決める。
- breadthの正本は`sourceHostCount`、表示sampleは最大12件の`sourceHosts`とする。
- raw URLやclickable linkは採用しない。host-only plain textを採用する。
- provider結果が揃った後のresults collected段階で表示し、検索前の対象siteとは表現しない。
- generic activity historyは復活させず、最新activity内だけでrotationする。
- intervalは800ms、reduced motionではrotationしない。
- source host表示のための新規dependencyは追加しない。

本仕様に実装を進められない未決事項はない。intervalや表示件数の将来調整は実測に基づくfollow-upとし、
本sliceの契約変更とは分ける。
