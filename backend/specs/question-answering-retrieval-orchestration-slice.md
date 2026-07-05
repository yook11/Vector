# QuestionAnsweringService retrieval orchestration slice 仕様

> 改訂: `retrieve()` の signature と `RetrievalOutcome` の形は
> `question-answering-plan-execution-separation-slice.md` で
> `retrieve(plan) -> RetrievalOutcome (plan なし)` に改訂済み。
> dispatch 表と不変条件の大半は本仕様のまま有効。

## 位置付け

planner 呼び出し slice の次。`QuestionAnsweringService` が planner から受け取った
`QuestionPlan` を `retrieval_mode` で dispatch し、internal 分岐だけ既存の
internal retrieval を実行して `RetrievalOutcome` を返すところまでを実装する。

Evidence 正規化、外部検索の具象実装、回答生成、`AnswerQuestionResult` の構築は
後続 slice の責務とする。

## Problem

planner が返す完成済み `QuestionPlan` を実行に移す orchestration がまだない。
internal 検索基盤 `InternalSearchService.search_plan_articles()` は存在するが、
agent 本体から呼ばれていない。

## Evidence

- `backend/app/agent/contract.py`
  - `RetrievalMode`: 4 値の closed Literal。dispatch の網羅対象。
  - `UnmetRequirement`: `"external_search"` を含む未充足要件の語彙。
  - `AnswerRetrievalSummary`: 最終 result 側が unmet を値で表現する前例。
- `backend/app/agent/answering/service.py`
  - `plan()` は walking skeleton 用の暫定 seam。呼び出し元はテストのみ
    (`app/` 側参照ゼロ) であり、本 slice で `retrieve()` に置き換える。
- `backend/app/agent/internal_retrieval/service.py`
  - `InternalSearchService.search_plan_articles(plan, *, per_query_limit=5,
    limit=5)` が `list[InternalArticleSearchHit]` を返す。キーワード引数は
    default 持ちのため、最小 signature の port にそのまま適合する。
  - `embed_plan_queries` に `retrieval_mode` ガードが既にあるが、dispatch の
    責務は answering 側に持たせ、下層ガードへ間接依存しない。
- `backend/tests/agent/answering/test_service.py`
  - `FakeQuestionPlanner` と既存 3 テスト (呼び出し 1 回 / 入力素通し /
    例外伝播)。`retrieve()` 前提に引き継ぐ。

## Invariants

- `QuestionPlan` / `RetrievalOutcome` は内部契約であり、API response として
  公開しない。`contract.py` は final result 用に保ち、`RetrievalOutcome` は
  answering package 内に置く。
- `retrieve()` は planner の返した `QuestionPlan` を書き換えず、そのまま
  `RetrievalOutcome.plan` に入れる。
- dispatch は `retrieval_mode` の `match` で 4 mode を網羅する。
- internal 検索を呼ぶのは `internal` / `internal_and_external` のみ。
  `none` / `external` では internal_search を呼ばない。
- `internal_and_external` は internal 検索を実行しつつ、external の未充足を
  記録する (取れるものは取り、取れなかった事実を残す)。
- 外部検索の未実装は例外や None ではなく
  `unmet_requirements=["external_search"]` として値で表現する。
- `internal_hits` は `search_plan_articles()` の返却順を保持する。
- planner / internal search の retry / fallback / audit / metrics は各層の
  責務とし、answering 側へ重複実装しない。
- planner / internal search が想定外例外を投げた場合、握りつぶさず伝播する。

## Non-goals

- external search port の定義 (Evidence 形が決まる後続 slice で切る)。
- `ExternalSearchService` の具象実装。
- Evidence 正規化 (EvidencePack 等の中間型)。
- source ref 採番 / citation / answer synthesis。
- `AnswerQuestionResult` の生成、`QuestionAnsweringAgent.answer()` の実装。
- FastAPI router / endpoint の追加、frontend 型生成。
- `per_query_limit` / `limit` のチューニング (default に委ねる)。
- `InternalSearchService` 側の実装変更。

## Changed Files

```text
backend/app/agent/answering/service.py   (plan() -> retrieve() 置き換え)
backend/tests/agent/answering/test_service.py
```

## Service Contract

```python
class InternalArticleRetriever(Protocol):
    async def search_plan_articles(
        self,
        plan: QuestionPlan,
    ) -> list[InternalArticleSearchHit]: ...


class RetrievalOutcome(BaseModel):
    """plan 実行の中間結果。Evidence 正規化前の生の retrieval 結果。"""

    model_config = ConfigDict(frozen=True)

    plan: QuestionPlan
    internal_hits: list[InternalArticleSearchHit] = Field(default_factory=list)
    unmet_requirements: list[UnmetRequirement] = Field(default_factory=list)


class QuestionAnsweringService:
    def __init__(
        self,
        *,
        planner: QuestionPlanner,
        internal_search: InternalArticleRetriever,
    ) -> None: ...

    async def retrieve(self, input: AnswerQuestionInput) -> RetrievalOutcome:
        ...
```

- `InternalArticleRetriever` は consumer 側 (answering) に定義する Protocol。
  実物は `InternalSearchService`。
- `retrieve()` は暫定 public seam とする。後続 slice で `answer()` が
  実装されたら内部ステップへ降格する。
- `plan()` seam は削除し `retrieve()` に更新する (暫定 seam を残さない)。

## Behavior

```text
QuestionAnsweringService.retrieve(input)
  -> planner.plan(input)
  -> match plan.retrieval_mode
      none:
        RetrievalOutcome(plan=plan)
      internal:
        hits = internal_search.search_plan_articles(plan)
        RetrievalOutcome(plan=plan, internal_hits=hits)
      external:
        RetrievalOutcome(plan=plan, unmet_requirements=["external_search"])
      internal_and_external:
        hits = internal_search.search_plan_articles(plan)
        RetrievalOutcome(
          plan=plan,
          internal_hits=hits,
          unmet_requirements=["external_search"],
        )
```

## Tests

`FakeQuestionPlanner` (既存) と `FakeInternalArticleRetriever` で検証する。

1. `retrieve()` が planner を 1 回呼び、`AnswerQuestionInput` をそのまま渡す。
2. `none`: internal_search が呼ばれず、`internal_hits` / `unmet_requirements`
   が空で `plan` がそのまま入る。
3. `internal`: internal_search が plan そのままで 1 回呼ばれ、返った hits が
   順序を保って `internal_hits` に入る。`unmet_requirements` は空。
4. `external`: internal_search が呼ばれず、
   `unmet_requirements == ["external_search"]`。
5. `internal_and_external`: internal_search が 1 回呼ばれ、hits と
   `unmet_requirements == ["external_search"]` の両方を持つ。
6. planner 例外は握りつぶさず伝播する (既存テストの引き継ぎ)。
7. internal_search 例外は握りつぶさず伝播する。

## Done

- `QuestionAnsweringService.retrieve(input)` が上記 dispatch を実装している。
- `plan()` seam が削除され、テストが `retrieve()` 前提に置き換わっている。
- `backend/tests/agent/answering/test_service.py` で 4 mode の呼び分けと
  例外伝播が確認できる。
- 既存 API endpoint、DB schema、planner 実装、`InternalSearchService` には
  変更を加えない。
