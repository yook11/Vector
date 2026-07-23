# Question Planner / Routing Spec

Status: Implemented
Updated: 2026-07-23
Scope: LLM planner が Direct Answer と Search を選び、回答実行へ完成済み plan を渡す契約

## Problem

質問への回答が新しい根拠を必要とするかを判定し、必要な場合は内部記事検索と外部リサーチを
同じ Search plan として計画する。planner は検索 topology の片側を選ばず、外部 keyword query の
生成も担当しない。

## Decisions

- `PlanType` は `direct_answer | search` の 2 値である。
- `direct_answer` は挨拶、使い方、文章変換、既存回答の表現変更など、根拠収集なしで完結する質問に使う。
- ニュース、企業、投資判断、規制、研究発表、鮮度または相対日時を含む事実質問は `search` にする。迷った場合も `search` にする。
- `search` は内部記事検索と外部リサーチを常に両方実行する。各入力は 1〜3 件である。
- `target_time_window` は外部根拠の公開・更新期間だけを表す。内部記事へ strict な期間 filter は加えない。
- 外部 keyword query は External Query Agent が `research_goal`、`as_of`、`target_time_window` から生成する。

## Planner Input

`PlanningRequest` は Question Context と request 開始時の `as_of` を渡す。`as_of` は相対時間表現の
解釈基準であり、prompt の untrusted input 境界を弱めない。

## Planner Draft Output

```python
class QuestionPlanDraft(BaseModel):
    plan_type: PlanType
    article_search_queries: list[str]
    research_goals: list[str]
    target_time_window: TargetTimeWindow | None = None
```

- `article_search_queries` は Vector の分析済み記事を semantic search するための query である。raw question を
  コピーせず、entity、topic、event、time intent を抽出・圧縮する。
- `research_goals` は外部で確認する根拠・観点であり、外部 keyword query ではない。
- `direct_answer` は両配列を空、期間を null にする。
- `search` は両配列を 1〜3 件にする。

## Prompt and Wire Schema

prompt と手書き response schema は上記の 2 plan と field を必須にする。`content_requirements` は検索対象・
比較軸へ反映し、`response_requirements` の形式・文体・簡潔さだけを理由に検索を増減させない。
conversation context は計画の文脈であり、事実根拠ではない。prompt version は `v3` である。

## Planning and Retry

`QuestionPlanningService` は planner runtime scope を 1 回開き、同じ client で最大 2 attempt を行う。

1. valid な completed plan ができれば返す。
2. JSON 不正、object 以外、schema 不一致、または plan の意味的不整合だけを response defect として
   repair input を渡し 2 回目を試す。
3. 2 回目の response defect、分類済み provider failure、unknown failure、cancellation は plan を作らず伝播し、
   回答 run を停止する。

質問文で query や goal を補完せず、fallback plan も作らない。最終失敗後は内部検索、外部リサーチ、
回答生成を開始しない。

## Search Dispatch

`AnsweringRunner` は Search plan で内部記事検索と外部リサーチを固定 2 枝として並行開始する。非 cancellation
時の枝例外は既存の内部優先規約で扱い、outer cancellation 時は両枝を cancel・join して捕捉した
`CancelledError` をそのまま再送出する。internal search の分類済み失敗は収集 failure として成功側の証拠を
保持し、その他の例外は停止する。

## Result Boundary

`AnswerQuestionResult.plan_summary` は `plan_type` と collection failures を持つ内部 summary である。
direct answer の answered 結果は source を持たず、Search の answered 結果は source を必要とする。
この summary は HTTP API、DB、frontend の公開 shape を変更しない。

## Non-goals

- 内部記事検索への期間 strict filter の追加
- External Query Agent、External Evidence Selector、Tavily の検索・選別 policy の変更
- planner による外部 keyword query の生成
- HTTP API、DB、Redis、Taskiq、frontend の変更
