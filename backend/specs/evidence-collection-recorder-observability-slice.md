# EvidenceCollection Recorder 観測集約 slice 仕様

更新日: 2026-08-29

実装状況: Implemented — 2026-08-29

## Work Definition

### Problem

EvidenceCollection、InternalSearch、ExternalSearch の span と metric の管理が各 Service の
`try/finally`へ分散し、工程の本来の処理と観測用の状態管理が混在していた。また、task、内部検索、
外部検索の親子関係が trace だけでは読み取りにくく、ExternalSearch の outcome counter も
工程の結論と実行状態を同じ属性に混在させていた。

### Invariants

- 公開する `EvidenceCollector.collect()`、`InternalSearch.search()`、`ExternalSearch.search()` の
  Protocol、戻り値、retry、縮退動作を変更しない。
- 記録障害は本処理へ伝播させず、本来の例外の同一性を維持する。
- query、research goal、検索結果、provider response、例外メッセージを span・metric に載せない。
- `collect()`一回、research task一件、各`search()`一回を、それぞれの記録単位にする。
- 分類済みの縮退結果と、未分類例外・キャンセルを区別する。

### Non-goals

- Live events、structured logs、非同期exportの変更。
- LLM/provider Recorder、Tavily HTTP span、補助metricの移管。
- 検索・retry・縮退・result assemblyの変更。

### Done

- Serviceは工程Recorderのcontext managerと`report_outcome()`だけを使う。
- traceの階層、metricの分類、例外同一性、機密情報の非露出がテストで確認される。

## Span contract

```text
agent_phase: phase=evidence_collection
├─ evidence_collection_task: task_index=0
│  ├─ internal_search
│  └─ external_search
│     ├─ agent_phase: phase=evidence_collection, agent_name=external_query_generator
│     │  └─ agent_provider_call
│     └─ external_search_call
└─ evidence_collection_task: task_index=1
   ├─ internal_search
   └─ external_search
```

- 外側の`agent_phase`は`collect()`一回につき一つとし、`task_index`を持たない。
- `evidence_collection_task`はresearch taskごとに一つ作り、ここだけが`task_index`を持つ。
- InternalSearch・ExternalSearchの操作spanは対応するtask spanの子になる。
- query生成の`agent_phase`はExternalSearch spanの子で、provider spanの親になる。
- Tavilyの`external_search_call`はGatewayが所有し、ExternalSearch spanの子になる。
- 分類済みの縮退結果は正常終了とし、未分類例外はERROR、キャンセルは停止として扱う。

## Recorder contract

### EvidenceCollection

`EvidenceCollectionRecorder.record()`は`collect()`全体を囲み、recording handleの
`record_task(task_index)`がtask単位のspanを囲む。EvidenceCollection固有のmetricやoutcome型は
持たない。

### InternalSearch

記録結論は次の閉じた型だけを受け取る。

- `InternalSearchSucceeded(hit_count)`
- `InternalSearchFailed(failure_code)`

`vector.agent.internal_retrieval.outcome`の既存属性契約は維持する。
`empty`は独立した工程結論ではなく、成功型の`hit_count == 0`をmetricへ変換するときだけ使う。
`hit_count`自体はmetric属性へ載せない。
分類済み失敗は`embedding_provider_failed | article_search_failed`の閉じたcodeへ写し、
既存counterには`failure_code`属性として載せる。例外自由文や旧属性は記録しない。

```text
vector.agent.internal_search.duration
  status: completed | failed | stopped
  outcome: succeeded | empty | failed | none
```

分類は、ヒットあり=`completed/succeeded`、0件=`completed/empty`、`InternalSearchError`=
`failed/failed`、未分類例外=`failed/none`、キャンセル=`stopped/none`とする。

### ExternalSearch

記録結論は次の閉じた型だけを受け取る。

- `ExternalSearchSucceeded`
- `ExternalSearchQueryGenerationFailed`
- `ExternalSearchProviderFailed`

```text
vector.agent.external_search.outcome
  result: succeeded | query_generation_failed | provider_failed

vector.agent.external_search.duration
  status: completed | failed | stopped
  outcome: succeeded | query_generation_failed | provider_failed | none
```

query生成失敗と全provider失敗は、工程が分類して戻り値を返すため`completed`とする。未分類例外は
`failed/none`、キャンセルは`stopped/none`とする。outcome counterは分類済み結論がある場合だけ
一回記録する。

## Verification policy

テストは公開された結果、spanの意味的な親子関係、metric属性、例外同一性、情報非露出を検証する。
内部helperの個数、特定の実装手順、trace全体に存在する同名spanの総数など、契約ではない実装形状は
固定しない。
