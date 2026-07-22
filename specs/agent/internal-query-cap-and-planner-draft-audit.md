# Article Search Query Cap and Planner Observability

Status: Implemented
Updated: 2026-07-23
Scope: article search query の正規化上限と planner の observability 境界

## Query Cap and Normalization

`MAX_ARTICLE_SEARCH_QUERIES = 3` は planner completed plan と `InternalSearchQueries` の共通上限である。
planner draft は blank、重複、上限超過を含みうるが、`plan_from_draft()` が strip、blank 除外、
casefold dedupe、入力順保持、先頭 3 件への cap を行う。completed `SearchPlan` はこの正規化済み入力だけを持つ。

`InternalSearchQueries` は leaf service の value object である。直接構築した blank query または上限超過は
validation error にし、空 tuple は既存 no-op 契約として許容する。実行側は plan を再正規化せず、
value object を構築して渡す。

## Planner Observability

planner に audit recorder または DB audit event はない。観測は次の境界に限定する。

- planner runtime attempt span: provider call ごとの安全な agent、model、attempt、prompt version、result/error を記録する。
- `vector.agent.planner.outcome`: completed planまたは分類済み最終failureごとに1回、
  `result=planned|failed`、`retry_used`、`plan_type=direct_answer|search|not_created`、
  `failure_code`を記録する。scope失敗、unknown、cancellationでは記録しない。

metric と span に raw question、prompt、response、query text、goal、provider exception 本文を入れない。
query embedding cache、article search repository、visibility projection、transaction と cache failure policy は
それぞれの内部検索仕様が所有する。

## Non-goals

- query embedding cache の schema、hash、transaction policy の変更
- planner observability の永続化や新規 event の追加
- API、DB、frontend の変更
