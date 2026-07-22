# Question Planner Failure Classification and Observability

Status: Implemented
Updated: 2026-07-23
Scope: planner の failure classification、retry、runtime span、outcome metric

## Failure Classification

planner は response defect だけを同一 request 内で retry する。対象は JSON 不正、object 以外、
wire schema 不一致、completed plan の意味的不整合である。最大 attempt 数は 2 で、repair input は
安全な分類情報だけを含む。

分類済み provider failure、runtime scope failure、unknown failure、cancellation は retry しない。
最終 response defect を含むすべての終端 failure は例外を伝播し、plan、検索、回答生成を開始しない。

## Runtime Span

planner phase は runtime attempt span と provider attempt span を使う。span は agent、model、attempt、
prompt version、固定された result/error 属性だけを記録する。raw question、prompt、model output、
provider exception 本文は記録しない。

## Outcome Metric

`vector.agent.planner.outcome` は completed planを返す場合、または分類済みの最終failureを伝播する場合に
1回だけ記録する。runtime scopeの開始・終了失敗、unknown failure、cancellationでは記録しない。

| Attribute | Values |
|---|---|
| `result` | `planned` / `failed` |
| `retry_used` | `true` / `false` |
| `plan_type` | `direct_answer` / `search` / `not_created` |
| `failure_code` | 固定の分類 code、成功時は `none` |

`planned` は completed plan を返した結果、`failed` は分類済みfailureでplanを作らず停止した結果である。
metric はrequest内の試行履歴や自由文を保存する仕組みではない。

## Non-goals

- audit recorder、DB event、best-effort audit transaction の導入
- plan failure 時の継続用 plan の生成
- provider 固有 failure policy の再定義
