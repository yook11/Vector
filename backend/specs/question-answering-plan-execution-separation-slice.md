# QuestionAnsweringService plan 実行分離 slice 仕様

## 位置付け

retrieval orchestration slice
(`question-answering-retrieval-orchestration-slice.md`) の改訂。

現状の `retrieve(input)` は plan 取得 (planner 呼び出し) と plan 実行 (検索起動)
を 1 メソッドに融合している。これを分離し、「planner は plan を返すまでが責務 /
answering は plan を受け取って実行する」を class 間だけでなく method signature と
中間型のレベルでも成立させる。

この slice が扱うのは回答の根拠となるデータ取得まで。取得データを根拠に回答を
生成する工程 (Evidence 正規化・synthesis・citation 照合) は後続 slice の責務と
する。

## Problem

- `retrieve(input: AnswerQuestionInput)` が内部で planner を呼んでから検索を
  起動しており、外から見ると planning と retrieval が 1 責務に見える。
- `RetrievalOutcome` が `plan` フィールドを echo しているため、「実行側が plan
  を返す」ように型が読める。plan を生産するのは planner だけであるべき。

責務分離は class 間 (planning / answering) では既に成立しているが、seam の形が
それを表現していない。

## Evidence

- `backend/app/agent/answering/service.py`
  - 現状: `retrieve(input)` + constructor の `planner` 依存 +
    `RetrievalOutcome.plan`。
  - `InternalArticleRetriever` port は本 slice でも現状のまま使う。
- `backend/app/agent/planning/service.py`
  - `QuestionPlanningService.plan()` は完成済み `QuestionPlan` を返すだけで
    検索に触れない。分離後の姿と整合済み。
- `backend/app/agent/contract.py`
  - `QuestionPlan` は frozen。実行側での書き換えは構造的に不可。
  - `UnmetRequirement` の語彙は変更しない。
- `backend/tests/agent/answering/test_service.py`
  - `FakeQuestionPlanner` + `FakeInternalArticleRetriever` で `retrieve(input)`
    を検証中。planner 系の検証は本 slice で retrieve の責務から外れる。
- `retrieve()` / `RetrievalOutcome` の呼び出し元は answering のテストのみ
  (`app/` 側参照ゼロ)。signature 変更は破壊なしで可能。

## Invariants

- planner の呼び出しは `retrieve()` の外に置く。本 slice 完了後、
  `QuestionAnsweringService` は planner に依存しない (後続の `answer()` slice
  で `plan -> retrieve -> synthesize` を束ねる際に依存が戻る)。
- `retrieve(plan)` は受け取った `QuestionPlan` を書き換えない。
- `RetrievalOutcome` は plan を含まない純粋な実行結果 (根拠データ + 未充足要件)
  とする。下流で plan が必要な場面へは、plan を保持する呼び出し側 (将来の
  `answer()`) が別引数で渡す。
- dispatch は `retrieval_mode` の `match` で 4 mode を網羅する。
- internal 検索を呼ぶのは `internal` / `internal_and_external` のみ。
  `none` / `external` では internal_search を呼ばない。
- `internal_and_external` は internal 検索を実行しつつ external の未充足を
  記録する。
- 外部検索の未実装は `unmet_requirements=["external_search"]` として値で
  表現する (例外・None にしない)。
- `internal_hits` は `search_plan_articles()` の返却順を保持する。
- internal search の retry / audit / metrics は internal_retrieval 側の責務と
  し、answering へ重複実装しない。想定外例外は握りつぶさず伝播する。
- `RetrievalOutcome` は内部型であり、API response として公開しない。
  `contract.py` には置かない。

## Non-goals

- Evidence 正規化 / source_ref 採番 / citation 照合 / answer synthesis。
- `AnswerQuestionResult` の生成、`QuestionAnsweringAgent.answer()` の実装。
- external search port の定義、`ExternalSearchService` の具象実装。
- planning / internal_retrieval / `contract.py` 側の変更。
- FastAPI router / endpoint の追加、frontend 型生成。
- `per_query_limit` / `limit` のチューニング。

## Changed Files

```text
backend/app/agent/answering/service.py
backend/tests/agent/answering/test_service.py
```

## Service Contract

```python
class RetrievalOutcome(BaseModel):
    """plan 実行の純粋な結果。回答の根拠候補データと未充足要件のみを持つ。"""

    model_config = ConfigDict(frozen=True)

    internal_hits: list[InternalArticleSearchHit] = Field(default_factory=list)
    unmet_requirements: list[UnmetRequirement] = Field(default_factory=list)


class QuestionAnsweringService:
    def __init__(self, *, internal_search: InternalArticleRetriever) -> None:
        ...

    async def retrieve(self, plan: QuestionPlan) -> RetrievalOutcome:
        ...
```

- constructor から `planner` 依存を外す (未使用依存を持たせない)。
- `retrieve()` は引き続き暫定 public seam。後続 slice で `answer()` が実装
  されたら内部ステップへ降格する。

## Behavior

```text
QuestionAnsweringService.retrieve(plan)
  -> match plan.retrieval_mode
      none:
        RetrievalOutcome()
      internal:
        hits = internal_search.search_plan_articles(plan)
        RetrievalOutcome(internal_hits=hits)
      external:
        RetrievalOutcome(unmet_requirements=["external_search"])
      internal_and_external:
        hits = internal_search.search_plan_articles(plan)
        RetrievalOutcome(
          internal_hits=hits,
          unmet_requirements=["external_search"],
        )
```

型が責務をそのまま語る形にする:

```text
planner:  AnswerQuestionInput -> QuestionPlan       (plan を返すまで)
retrieve: QuestionPlan        -> RetrievalOutcome   (plan を受けて実行)
```

## Tests

`FakeInternalArticleRetriever` のみで検証する。`FakeQuestionPlanner` と
planner 系テスト (呼び出し 1 回 / 入力素通し / 例外伝播) は retrieve の責務から
外れるため本 slice で削除する (planner との結線検証は `answer()` slice で
復活させる)。

plan はテスト内で直接構築して渡す。

1. `none`: internal_search が呼ばれず、`internal_hits` / `unmet_requirements`
   がともに空。
2. `internal`: internal_search が渡した plan そのままで 1 回呼ばれ、返った
   hits が順序を保って `internal_hits` に入る。`unmet_requirements` は空。
3. `external`: internal_search が呼ばれず、
   `unmet_requirements == ["external_search"]`。
4. `internal_and_external`: internal_search が 1 回呼ばれ、hits と
   `unmet_requirements == ["external_search"]` の両方を持つ。
5. internal_search 例外は握りつぶさず伝播する。

## Done

- `retrieve(plan: QuestionPlan) -> RetrievalOutcome` になっている。
- `QuestionAnsweringService` の constructor に `planner` 依存がない。
- `RetrievalOutcome` に `plan` フィールドがない。
- `backend/tests/agent/answering/test_service.py` が上記 5 ケースで green。
- planning / internal_retrieval / `contract.py` / API には変更を加えない。
