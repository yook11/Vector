# QuestionAnsweringService answer() orchestration slice 仕様 (Slice A)

## 位置付け

Q&A エージェントの最上位ユースケース `QuestionAnsweringService.answer(input)` を
新設し、工程 1〜4 を 1 本の直列に繋ぐ。工程 4 (`AnswerSynthesizer`) は port と
draft 契約のみ定義し、実 LLM adapter・retry・audit・metrics は Slice B の
責務とする。本 slice の検証は全て fake synthesizer で行う。

| 工程 | 名前 | module | 状態 |
|---|---|---|---|
| ユースケース `answer(input)` | `QuestionAnsweringService` | `answering/service.py` (新) | **本 slice で新設** |
| 工程 1: プラン作成 | `QuestionPlanningService` | `planning/` | 実装済み |
| 工程 2: plan を読んで検索起動 | `QuestionPlanRetrievalService` | `answering/retrieval.py` (rename) | 実装済み |
| 工程 3: evidence 正規化 | `normalize_answer_evidence` | `answering/evidence.py` | 実装済み |
| 工程 4: 回答文合成 | `AnswerSynthesizer` | `answering/synthesis.py` (新) | **本 slice で port のみ**、実装は Slice B |

module rename: 現 `answering/service.py` (工程 2 + `RetrievalOutcome` + ports) を
`answering/retrieval.py` に rename し、`service.py` はユースケースに明け渡す。
これは import cycle の回避を兼ねる: `evidence.py` が `RetrievalOutcome` を
service.py から import しているため、service.py に use case を置いたまま
`normalize_answer_evidence` を import すると循環する。rename 後は
`service.py (use case) -> retrieval.py / evidence.py / synthesis.py` の
一方向依存になり、module 名も工程対応表と 1:1 になる。

## Problem

- 工程 1〜3 は完成しているが、束ねる者がいない (`normalize_answer_evidence`
  はまだ誰からも呼ばれていない)。
- 回答生成の契約 (draft の形、citation 照合、`AnswerQuestionResult` の
  組み立て規則) が未定義。
- 外部検索未実装 phase の暫定 helper `external_unavailable_result` が
  planning 側に残っており、answering の責務が planning に漏れたまま。

## Evidence

- `backend/app/agent/planning/contract.py` — **`QuestionPlan` は 4 variant の
  union** (`NoRetrievalPlan` / `InternalRetrievalPlan` / `ExternalSearchPlan` /
  `InternalAndExternalPlan`、全て `extra="forbid"`)。
  - `target_time_window` は `ExternalSearchPlan` / `InternalAndExternalPlan`
    のみが持つ。No/Internal variant への属性アクセスは AttributeError。
  - `plan_from_draft` の match + `assert_never` が variant 分岐の既存流儀。
- `backend/app/agent/contract.py`
  - `QuestionAnsweringAgent.answer()` が public port。本 slice の
    `QuestionAnsweringService` はこの Protocol を満たす。
  - `AnswerQuestionResult` の provenance validator:
    answered は missing_aspects / unmet_requirements を持てない。
    answered + non-direct は sources 必須。answered + used_external_search
    は external source 必須。`AnswerExecutionSummary` は route と used flags
    の整合を検証。
  - `AnswerExecutionSummary` の docstring は「実際に通った主要経路」の
    ままなので、引用由来の意味論に合わせて更新する (shape は変えない)。
- `backend/app/agent/answering/` — `QuestionPlanRetrievalService.retrieve()`
  / `RetrievalOutcome` / `normalize_answer_evidence` / `AnswerEvidenceItem`。
  `evidence.py` は `RetrievalOutcome` を service.py から import している
  (rename の動機)。
- `backend/app/agent/planning/service.py`
  - `QuestionPlanningService` の retry → `safe_fallback_plan` → audit 構造
    (Slice B が synthesis 側に鏡写しする対象)。
  - `external_unavailable_result` (本 slice で退役)。production 呼び出し
    ゼロ・re-export とテストのみ。
- `backend/app/agent/external_search/contract.py` —
  `ResearchTaskReport.missing`。`ExternalSearchOutcome` は report の
  index 集合の整合は検証するが list order は保証しない
  (missing の flatten に整列が必要な理由)。

## 合意済みの設計判断

1. **status 判定は決定的前段 + LLM 自己申告の併用**。自己申告の扱いは
   非対称: insufficient 申告はそのまま採用 (安全側を覆さない)、answered
   申告は citation 照合で裏を取る。
2. **direct (`NoRetrievalPlan`) を含める**。特別経路は作らず、同じ直列を
   空 evidence が流れる。
3. **不正 draft は typed error** (`AnswerDraftInvalidError`)。黙って修復
   しない。retry / fallback で「処理を止めない」のは Slice B の synthesis
   service が巻く。
4. **execution summary は引用由来の意味論**。`used_internal_retrieval` /
   `used_external_search` は「引用された evidence の種類」、route はその組
   から導出。「外部検索を実行したが回答に使わなかった」は
   used_external=False の answered として valid に構築できる。実行したが
   不使用だった事実は audit / metrics (Slice B) で観測する。contract の
   docstring もこの意味論に更新する。
5. **sources は引用された item のみ** (`.source` を集めるだけ。再構築・
   再採番しない)。
6. **missing_aspects は最終不足理由に固定**。途中経過の可視化
   (progress event / timeline) は別設計とし本 slice では扱わない。

## New Types / Contracts

`backend/app/agent/answering/synthesis.py` (新規):

```python
class AnswerDraft(BaseModel):
    """Synthesizer (LLM 工程) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    sufficiency: Literal["answered", "insufficient"]
    answer: str = Field(min_length=1)
    cited_refs: list[str] = Field(default_factory=list)
    missing_aspects: list[str] = Field(default_factory=list)

    # model_validator: answered の draft は missing_aspects を持てない
    # (contract の answered 制約を draft 段階で構造的に先取り)。


class AnswerSynthesizer(Protocol):
    async def synthesize(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
    ) -> AnswerDraft: ...


class AnswerDraftInvalidError(Exception):
    """draft が evidence への接地契約を破ったことを表す typed error。"""
```

`backend/app/agent/answering/service.py` (rename 後に新規作成):

```python
class QuestionPlanRetriever(Protocol):
    async def retrieve(
        self, plan: QuestionPlan, *, as_of: datetime
    ) -> RetrievalOutcome: ...


class QuestionAnsweringService:
    """質問に答えるユースケース。工程の順序のみを所有する。"""

    def __init__(
        self,
        *,
        planner: QuestionPlanner,
        retriever: QuestionPlanRetriever,
        synthesizer: AnswerSynthesizer,
    ) -> None: ...

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult:
        ...
```

plan variant の読み取り helper (answering 側、match + `assert_never`):

```python
def _plan_target_time_window(plan: QuestionPlan) -> str | None:
    match plan:
        case ExternalSearchPlan() | InternalAndExternalPlan():
            return plan.target_time_window
        case NoRetrievalPlan() | InternalRetrievalPlan():
            return None
    assert_never(plan)
```

## Behavior

```text
answer(input)
  1. plan    = planner.plan(input)
  2. outcome = retriever.retrieve(plan, as_of=input.as_of)
  3. evidence = normalize_answer_evidence(outcome)
  4. 決定的前段:
       plan が NoRetrievalPlan でなく、かつ evidence が空
         -> synthesizer を呼ばず insufficient result を組み立てて返す
            answer  = 定型文
            sources = []
            missing = retrieval-empty 定型 -> unmet 定型
                      -> task_reports.missing の順で連結 (必ず 1 件以上)
  5. draft = synthesizer.synthesize(
       question=input.question, evidence=evidence,
       as_of=input.as_of,
       target_time_window=_plan_target_time_window(plan))
  6. citation 照合 (不正なら AnswerDraftInvalidError):
       - cited_refs に evidence に存在しない ref がある
       - plan が NoRetrievalPlan でなく sufficiency="answered" かつ
         cited_refs が空 (根拠があるのに接地しない answered は拒否)
  7. 組み立て (決定的):
       sources  = 引用された item の .source (採番順、cited_refs の重複は一意化)
       used_*   = 引用に internal / external が含まれるか
       route    = (used_internal, used_external) から導出:
                  (F,F)->direct (T,F)->internal (F,T)->external_search
                  (T,T)->internal_and_external
       status   = "insufficient" if (unmet_requirements 非空
                   or draft.sufficiency=="insufficient") else "answered"
       missing  = status=="insufficient" のとき
                  unmet 定型句
                  -> task_reports.missing (task_index 昇順に整列して flatten)
                  -> draft.missing_aspects
                  の順で連結、完全一致 dedup (先勝ち)。answered のときは空。
       answer   = draft.answer (決定的前段のみ定型文)
       retrieval = AnswerRetrievalSummary(planned_mode=plan.retrieval_mode,
                                          unmet_requirements=outcome.unmet_requirements)
```

status 規則の補足: `unmet_requirements` 非空 → 常に insufficient は
contract の validator (answered は unmet を持てない) から導かれる規則。
計画した取得が満たせなかった場合、draft が answered を申告しても
「完全回答」を名乗らず、draft.answer を部分回答として insufficient で返す。

## Invariants

- `answer()` は工程の順序のみ所有し、mode 分岐を持たない (dispatch は
  工程 2 に封じたまま)。direct 判定と target_time_window の読み取りだけを
  variant match (helper) で行い、`hasattr` / `getattr` に頼らない。
- `as_of` は `input.as_of` を素通しし、再生成しない。
- answered は「draft の自己申告 + citation 照合の裏取り」でのみ成立する。
  insufficient の自己申告は覆さない。
- `unmet_requirements` 非空の result は必ず insufficient。
- 根拠ゼロ (NoRetrievalPlan 以外) では synthesizer を呼ばない。この
  決定的前段の result は **必ず 1 件以上の missing_aspects を持つ**
  (retrieval-empty 定型を先頭に追加する)。
- 不正 draft は黙って修復せず `AnswerDraftInvalidError` を raise して
  伝播させる (retry / fallback / audit / metrics は Slice B。本 slice で
  重複実装しない)。
- sources は引用された item の `.source` のみ。source_ref の再採番・
  provenance の再構築をしない。
- execution summary (used_* / route) は引用由来。検索の実行有無からは
  導出しない。
- missing_aspects は最終不足理由のみ。`task_reports.missing` は
  task_index 昇順に整列してから flatten する。進行状況は含めない。
- 各工程 (planner / retriever / synthesizer) の想定外例外は握りつぶさず
  伝播する。
- `contract.py` の shape (field / validator) は変更しない。
  `AnswerExecutionSummary` の docstring のみ引用由来の意味論に更新する。
  定型文言は answering 側の実装定数とする。

## Non-goals

- 実 LLM adapter / synthesis の retry / fallback / audit / metrics (Slice B)。
- probe script の `answer()` 延長 (Slice B の貫通で行う)。
- API endpoint / FastAPI DI / frontend 型生成。
- progress event / timeline (途中経過の可視化) の設計。
- synthesizer への不足ヒント (task_reports.missing 等) の供給
  (プロンプト品質の話として Slice B 以降で必要なら port を拡張)。
- `plan_question` compatibility helper の整理。
- 定型文言の作り込み (規則のみ仕様化し、文言は実装で確定)。
- `contract.py` の shape 変更・plan variant 定義の変更。

## Changed Files

```text
backend/app/agent/answering/retrieval.py        (service.py から rename:
                                                 工程2 + RetrievalOutcome + ports)
backend/app/agent/answering/service.py          (新規: QuestionAnsweringService +
                                                 QuestionPlanRetriever + helper)
backend/app/agent/answering/synthesis.py        (新規: draft / port / error)
backend/app/agent/answering/evidence.py         (import 追従)
backend/app/agent/answering/__init__.py         (export 追加 + import 追従)
backend/app/agent/contract.py                   (AnswerExecutionSummary docstring のみ)
backend/app/agent/planning/service.py           (external_unavailable_result 削除)
backend/app/agent/planning/__init__.py          (export 削除)
backend/app/agent/__init__.py                   (export 削除)
backend/scripts/probe_question_answering.py     (import 追従)
backend/tests/agent/answering/test_retrieval.py (test_service.py から rename・追従)
backend/tests/agent/answering/test_service.py   (新規: answer() のテスト)
backend/tests/agent/planning/test_planner.py    (退役テスト削除)
```

## Tests

fake planner / retriever / synthesizer で検証する。期待値は fixture から
導出し、route / status は本仕様の規則から決める。plan は variant
constructor で直接構築する。

### 正常系 (status / route / sources)

1. internal answered: internal evidence 2 件を引用 → answered、sources が
   引用 item の `.source` と一致 (採番順)、route="internal" (T,F)、
   missing_aspects 空。
2. external answered: external のみ引用 → route="external_search" (F,T)、
   sources に external source (contract の external 必須 validator を通る)。
3. internal_and_external answered で両方引用 → route="internal_and_external"。
4. **引用由来意味論の要**: `InternalAndExternalPlan` で external evidence も
   存在するが internal のみ引用 → answered、used_external=False、
   route="internal"、sources に external が載らない。
5. direct: `NoRetrievalPlan` → synthesizer が空 evidence +
   target_time_window=None で呼ばれ、引用ゼロの answered draft →
   route="direct"、sources 空で answered が構築できる。

### insufficient 系

6. 決定的前段: `InternalRetrievalPlan` で hits ゼロ → synthesizer が
   呼ばれず insufficient (定型 answer、sources 空)、missing_aspects に
   retrieval-empty 定型が入り空でない。
7. unmet cap: `InternalAndExternalPlan` で external unmet、internal を
   引用した answered draft → 最終 status は insufficient、missing_aspects
   に unmet 定型句を含み、answer は draft.answer (部分回答)。
8. 自己申告採用: draft が insufficient + missing_aspects + 部分引用 →
   insufficient、引用分は sources に載り、draft の missing が含まれる。
9. missing 連結: unmet 定型 + task_reports.missing (task_index 昇順、
   report を逆順で与えて整列を検証) + draft.missing がこの順で連結され、
   完全一致の重複が除去される。

### 不正 draft / 型

10. evidence に存在しない ref を引用 → `AnswerDraftInvalidError`。
11. `NoRetrievalPlan` 以外で answered なのに引用ゼロ →
    `AnswerDraftInvalidError`。
12. cited_refs の重複 → sources は一意・採番順。
13. `AnswerDraft` validator: answered + missing_aspects → ValidationError。

### 配線 / 伝播

14. 工程素通し: planner に input がそのまま渡り、retriever に plan と
    input.as_of、synthesizer に question / evidence / input.as_of が渡る。
    target_time_window は `InternalAndExternalPlan` では plan の値、
    `InternalRetrievalPlan` では None が渡る (helper の variant 分岐検証)。
15. planner / retriever / synthesizer の例外が伝播する。

### 退役

16. `external_unavailable_result` の削除 (参照ゼロ、専用テスト削除)。

## Done

- `QuestionAnsweringService.answer(input)` が上記 Behavior を実装し、
  `QuestionAnsweringAgent` Protocol を満たす。
- module 構成が `retrieval.py / evidence.py / synthesis.py / service.py` に
  なり、一方向依存で import cycle がない。
- `AnswerDraft` / `AnswerSynthesizer` / `AnswerDraftInvalidError` が
  synthesis.py にあり、fake で answer() の全経路が検証できる。
- `external_unavailable_result` が削除され、参照ゼロ。
- 上記テストが green。既存 suite に regression なし。
- `contract.py` の shape / API endpoint / DB schema には変更を加えない
  (docstring 更新のみ)。
