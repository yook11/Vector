# Question Plan Variant Types

Status: Implemented
Updated: 2026-07-23
Scope: planner draft と completed plan の domain contract

## Domain Types

```python
PlanType = Literal["direct_answer", "search"]

class DirectAnswerPlan(BaseModel):
    plan_type: Literal["direct_answer"] = "direct_answer"

class ExternalResearchTask(BaseModel):
    research_goal: str

class SearchPlan(BaseModel):
    plan_type: Literal["search"] = "search"
    article_search_queries: list[PlanQuery]  # 1..MAX_ARTICLE_SEARCH_QUERIES
    external_research_tasks: list[ExternalResearchTask]  # 1..3
    target_time_window: TargetTimeWindow | None = None

QuestionPlan = DirectAnswerPlan | SearchPlan
```

すべての型は frozen かつ `extra="forbid"` である。`PlanType` の SSoT は `app.agent.contract` であり、
planning contract は同じ object を export する。

## Exact Fields and Invariants

- `DirectAnswerPlan` は plan type 以外の field を持たない。
- `SearchPlan` は分析済み記事用 query と外部 research task の両方を必須にする。
- query は case-insensitive に一意、research goal は文字列として一意である。
- query と task は各 1〜3 件である。`MAX_ARTICLE_SEARCH_QUERIES` は記事検索 query の上限である。
- `target_time_window` は Search plan だけに属し、外部検索の期間解釈に使う。

## Draft Normalization

`plan_from_draft()` は次の順で completed plan を作る。

1. query と goal を strip し、blank を除外する。
2. query は casefold、goal は strip 後の値で重複を除外し、最初の表記と順序を保つ。
3. 両方を先頭 3 件に制限する。
4. direct answer の非空入力または期間、Search の query または goal 不足は安全な response defect にする。
5. valid な goal を `ExternalResearchTask(research_goal=...)` に変換する。

completed plan の consumer は正規化を再実装しない。runner は Search plan の query を
`InternalSearchQueries` value object として内部検索へ渡し、task を外部リサーチへ渡す。

## Failure Boundary

semantic defect は planner response defect として同 scope の repair retry に参加する。質問文を入力へ
流用する補完や、別 plan を返す fallback は行わない。最終 failure は呼出側へ伝播する。

## Non-goals

- API、DB、frontend の型変更
- plan 以外の query embedding、article repository、外部検索 policy の変更
