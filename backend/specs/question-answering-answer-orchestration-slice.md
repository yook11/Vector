# QuestionAnsweringService answer() orchestration slice 仕様 (Slice A / rev.2)

## 改訂 (2026-07-07 rev.2)

rev.1 (単一直列、commit 5a96c40c で実装済み) からの設計改訂。入力は 2 つ。

### A. 回答経路の plan variant 分割 (合意済み)

旧合意「direct (`NoRetrievalPlan`) も同じ直列を空 evidence で流す。特別経路は
作らない」(rev.1 設計判断 2) を撤回する。撤回の根拠は rev.1 の実装で判明した事実:

1. 「特別経路なし」の建前の下で direct 分岐が 3 箇所に分散した
   (service の決定的前段ガード `_is_direct_plan` / 「answered は引用必須、
   ただし direct を除く」という条件付き citation 契約 / retriever の
   `NoRetrievalPlan` → 空 outcome という no-op case)。分岐は消えておらず、
   bool helper と条件付き契約に姿を変えて残った。
2. Slice B で 1 つの synthesizer プロンプトが「evidence に厳密接地して
   引用せよ」と「evidence なしで自然に答えよ」の対立する両モードを持つ
   ことになり、プロンプト内に型から見えない mode 分岐が生まれる。
3. 2 つの回答工程は失敗条件が違う (接地違反・根拠ゼロ・unmet は direct に
   存在しない)。同じ port 契約に押し込むのが歪みの源であり、分割は抽象化の
   追加ではなく誤った統一の解除。

### B. spec レビュー findings の反映 (合意済み)

- **P1 (理由なし insufficient)**: draft 自己申告経路だけ missing_aspects の
  下限がなく、missing 空の insufficient result が構築できた。
  → `AnswerDraft` に「insufficient → missing_aspects 非空」validator を追加し、
  `AnswerQuestionResult` にも backstop として同 validator を追加する。
  LLM 生出力がこの欠陥を持つ場合の扱いは planner 鏡写しの二層構造
  (工程内で決定的補完 + defect 記録、処理は止めない) とし、Slice B へ
  申し送る (後述)。
- **P2 (blank 回答のすり抜け)**: `min_length=1` だけでは空白のみ文字列が
  通る。→ direct 工程の出力を `DirectAnswerDraft` (non-blank answer) に
  型化し、`AnswerDraft` の answer / missing_aspects 要素にも同じ
  non-blank 制約を課す。final result の SSoT である `AnswerQuestionResult`
  にも backstop として同じ non-blank 制約を課す。
- **P3 (route="direct" の二義性)**: 「internal を計画したがヒットゼロ」に
  route="direct" が付き経路名と衝突する。根本原因は execution summary
  (route / used_*) が **sources から完全に導出可能な派生ラベル**であり、
  一次事実を持たないままドメイン result に整形済みラベルを焼き込んで
  いたこと。→ `AnswerExecutionSummary` / `ExecutionRoute` を contract から
  削除する。接地の一次データは sources、経路判別は
  `retrieval.planned_mode == "none"`、表示ラベル / metrics 次元は
  consumer 側 (API schema 層 / audit emit 時) で導出する。
  消費者が agent module 内部とテストのみであることは grep で確認済み。

### rev.1 実装 (commit 5a96c40c) からの delta

- `answer()` を経路 dispatch 化 (match 1 箇所)。`_is_direct_plan` 削除。
- `DirectAnswerer` + `DirectAnswerDraft` 新設 (`direct.py`)。
- `AnswerSynthesizer` → `EvidenceAnswerSynthesizer` rename。`AnswerDraft` の
  validator 強化 (answered → cited_refs 非空 / insufficient → missing 非空 /
  non-blank 制約)。
- planning contract に `RetrievalPlan` sub-union alias を追加し、retriever
  port の入力を絞る (`NoRetrievalPlan` no-op case を型で排除)。
- contract.py から `ExecutionRoute` / `AnswerExecutionSummary` /
  `execution` field を削除し、provenance validator を planned_mode ベースに
  書き換え + insufficient / non-blank backstop 追加。

## 位置付け

Q&A エージェントの最上位ユースケース `QuestionAnsweringService.answer(input)`
が plan variant を見て回答経路を選択し、各経路の工程を直列に繋ぐ。
回答工程 (4E / 4D) は port と draft 契約のみ定義し、実 LLM adapter・
プロンプト・retry・補完・audit・metrics は Slice B の責務とする。
本 slice の検証は全て fake で行う。

| 工程 | 名前 | module | 状態 |
|---|---|---|---|
| ユースケース `answer(input)` | `QuestionAnsweringService` | `answering/service.py` | rev.2 で dispatch 化 |
| 工程 1: プラン作成 | `QuestionPlanningService` | `planning/` | 実装済み |
| 工程 2: plan を読んで検索起動 | `QuestionPlanRetrievalService` | `answering/retrieval.py` | rev.2 で入力を `RetrievalPlan` に絞る |
| 工程 3: evidence 正規化 | `normalize_answer_evidence` | `answering/evidence.py` | 実装済み |
| 工程 4E: evidence 回答 | `EvidenceAnswerSynthesizer` | `answering/synthesis.py` | rev.2 で rename + validator 強化 |
| 工程 4D: direct 回答 | `DirectAnswerer` | `answering/direct.py` (新) | **rev.2 で port 新設**、実装は Slice B |

module 構成は `retrieval.py / evidence.py / synthesis.py / direct.py /
service.py` の一方向依存 (rev.1 で service.py → retrieval.py rename 済み。
import cycle 回避と工程対応 1:1 の維持が目的)。

## Problem

- rev.1 の単一直列は direct 分岐を消せておらず、3 箇所に分散させている
  (改訂 A の根拠 1)。
- evidence 回答と direct 回答で失敗条件が違うのに port 契約が 1 つのため、
  「direct を除く」例外付き契約になっている。
- result 契約に 3 つの穴がある: 理由なし insufficient (P1)、空白のみ回答
  (P2)、引用ゼロ結果への "direct" 誤ラベル (P3)。

## Evidence

- `backend/app/agent/planning/contract.py` — `QuestionPlan` は 4 variant の
  union (`NoRetrievalPlan` / `InternalRetrievalPlan` / `ExternalSearchPlan` /
  `InternalAndExternalPlan`、全て `extra="forbid"`、PEP 695 `type` alias)。
  - `target_time_window` は `ExternalSearchPlan` / `InternalAndExternalPlan`
    のみが持つ。match + `assert_never` が variant 分岐の既存流儀。
  - `plan_from_draft` は LLM 生 draft の欠陥 (query 空など) を raise せず
    決定的に補完する (`or [fallback_query]`)。**LLM 生出力 (lenient) →
    決定的補完 → 完成契約 (strict) の二層構造の前例** (P1 の Slice B 方針は
    この鏡写し)。
  - `PlanQuery` = `Annotated[str, StringConstraints(strip_whitespace=True,
    min_length=1)]` が non-blank constrained str の既存流儀 (P2 で踏襲)。
- `backend/app/agent/answering/service.py` (rev.1 実装) — `_is_direct_plan`
  が 2 箇所で分岐に使われ、citation 検証が条件付き。used_* は sources から
  計算しており (`_assemble_result`)、execution summary が派生値である証拠。
- `backend/app/agent/answering/retrieval.py` — `retrieve()` の
  `case NoRetrievalPlan(): return RetrievalOutcome()` が no-op dead path。
- `backend/app/agent/contract.py`
  - `QuestionAnsweringAgent.answer()` が public port。
    `QuestionAnsweringService` はこの Protocol を満たす。
  - `AnswerQuestionResult.answer` は `Field(min_length=1)` のみで
    `str_strip_whitespace` なし → 空白のみ文字列が通る (P2 の穴)。
  - insufficient 側の validator が存在せず missing 空の insufficient が
    valid (P1 の穴)。
  - `ExecutionRoute` / `AnswerExecutionSummary` の消費者は agent module
    内部と tests のみ (API schema / frontend / audit から参照ゼロ、
    grep 確認済み)。`"workers"` 値は投機的 placeholder。
- `backend/scripts/probe_question_answering.py` — external plan を直接構築
  して `QuestionPlanRetrievalService` に渡しており、入力型の縮小に影響
  されない (実装時に型チェックで確認する)。

## 合意済みの設計判断

1. **回答経路は plan variant で分割する**。`NoRetrievalPlan` → direct 経路
   (`DirectAnswerer`)、残り 3 variant → evidence 直列 (retrieve →
   normalize → synthesize)。dispatch は `answer()` の match 1 箇所のみが
   所有する。
2. **plan 型は増やさない・改名しない**。既存 4 variant がそのまま判別子。
   sub-union alias `RetrievalPlan` を planning contract に追加し
   `QuestionPlan = NoRetrievalPlan | RetrievalPlan` に再構成する。
   retriever port は `RetrievalPlan` のみ受ける。
3. **`DirectAnswerer` の契約は最小**。`question` と `as_of` を受けて
   `DirectAnswerDraft` (non-blank answer 1 field) を返すのみ。
   sufficiency / citation / missing を持たず、result は常に answered /
   sources 空 / planned_mode="none"。「direct なのに答えられない」は
   planner の誤ルーティングであり、result の status ではなく Slice B の
   audit / eval で観測する (direct 側に逃げ道を与えると planner の誤判定
   シグナルが見えなくなる)。
4. **status 判定 (evidence 経路) は決定的前段 + LLM 自己申告の併用**。
   自己申告の扱いは非対称: insufficient 申告はそのまま採用、answered 申告
   は citation 照合で裏を取る。
5. **draft の完全性は validator で構造的に保証する**。`AnswerDraft` は
   answered → cited_refs 非空・missing 空 / insufficient → missing 非空 /
   answer・missing 要素は non-blank。LLM 生出力がこの契約を満たさない
   ケースの扱いは工程内の責務 (Slice B): planner (`plan_from_draft`)
   鏡写しの二層構造で、補完可能な欠陥 (insufficient + missing 空) は
   定型補完 + defect 記録して処理を止めず、補完不能な欠陥は retry →
   fallback。**use case は valid な draft しか見ない**。
6. **execution summary は持たない** (P3)。route / used_* は sources から
   完全に導出可能な派生ラベルであり、result に一次事実を足さない。
   接地の一次データ = sources、経路判別 = `planned_mode == "none"`、
   表示ラベル / metrics 次元は consumer 側 (API schema 層 / audit emit 時)
   で導出する (整形は consumer 側・emit は決定境界の所有者、という既存
   feedback と同方針)。
7. **sources は引用された item のみ** (`.source` を集めるだけ。再構築・
   再採番しない)。
8. **missing_aspects は最終不足理由に固定**。途中経過の可視化
   (progress event / timeline) は別設計。
9. **両経路の result 組み立てを共通 helper に寄せない**。direct 側は
   自明な固定 shape の構築であり、重複排除目的の共有抽象を作らない。
10. **プロンプトは経路別に分離する** (Slice B)。本 slice は port 分離で
    その前提を確定する。

## New Types / Contracts

`backend/app/agent/planning/contract.py` (追加):

```python
type RetrievalPlan = (
    InternalRetrievalPlan | ExternalSearchPlan | InternalAndExternalPlan
)
type QuestionPlan = NoRetrievalPlan | RetrievalPlan
```

`backend/app/agent/contract.py` (変更 — 変更点はこの列挙が全て):

```python
# 削除: ExecutionRoute / AnswerExecutionSummary / AnswerQuestionResult.execution
# (__all__ も追従)

NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]

class AnswerQuestionResult(BaseModel):
    status: Literal["answered", "insufficient"]
    answer: NonBlankText
    sources: list[AnswerSource] = Field(default_factory=list)
    missing_aspects: list[NonBlankText] = Field(default_factory=list)
    retrieval: AnswerRetrievalSummary

    # validator (書き換え + 追加):
    #   answer / missing_aspects 要素は non-blank (新規: P2 backstop)
    #   answered → missing_aspects 空 / unmet_requirements 空 (維持)
    #   answered ∧ planned_mode != "none" → sources 非空 (route 参照から書き換え)
    #   planned_mode == "none" → sources 空 (新規: 検索なしに接地 source は在り得ない)
    #   insufficient → missing_aspects 非空 (新規: P1 backstop)
```

`backend/app/agent/answering/synthesis.py` (rename + validator 強化):

```python
class AnswerDraft(BaseModel):
    """Evidence 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    sufficiency: Literal["answered", "insufficient"]
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    cited_refs: list[str] = Field(default_factory=list)
    missing_aspects: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    ] = Field(default_factory=list)

    # model_validator:
    #   answered → missing_aspects 空 (既存)
    #   answered → cited_refs 非空 (rev.2: service の条件分岐から昇格)
    #   insufficient → missing_aspects 非空 (rev.2: P1)


class EvidenceAnswerSynthesizer(Protocol):
    """evidence に接地し引用付きで回答する工程 (旧 AnswerSynthesizer)。"""

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

`backend/app/agent/answering/direct.py` (新規):

```python
class DirectAnswerDraft(BaseModel):
    """Direct 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DirectAnswerer(Protocol):
    """検索なしで自然に回答する工程。evidence を受け取らない。"""

    async def answer(
        self, *, question: str, as_of: datetime
    ) -> DirectAnswerDraft: ...
```

`backend/app/agent/answering/service.py` (dispatch 化):

```python
class QuestionPlanRetriever(Protocol):
    async def retrieve(
        self, plan: RetrievalPlan, *, as_of: datetime
    ) -> RetrievalOutcome: ...


class QuestionAnsweringService:
    """質問に答えるユースケース。経路の選択と工程の順序のみを所有する。"""

    def __init__(
        self,
        *,
        planner: QuestionPlanner,
        retriever: QuestionPlanRetriever,
        synthesizer: EvidenceAnswerSynthesizer,
        direct_answerer: DirectAnswerer,
    ) -> None: ...
```

plan variant の読み取り helper は `RetrievalPlan` を受ける形に縮小:

```python
def _plan_target_time_window(plan: RetrievalPlan) -> str | None:
    match plan:
        case ExternalSearchPlan() | InternalAndExternalPlan():
            return plan.target_time_window
        case InternalRetrievalPlan():
            return None
    assert_never(plan)
```

## Behavior

```text
answer(input)
  1. plan = planner.plan(input)
  2. 経路 dispatch (唯一の variant match):
       match plan:
           case NoRetrievalPlan():
               -> direct 経路
           case InternalRetrievalPlan() | ExternalSearchPlan()
                | InternalAndExternalPlan():
               -> evidence 経路 (この arm で plan は RetrievalPlan に narrow)
       assert_never(plan)

direct 経路:
  draft = direct_answerer.answer(question=input.question, as_of=input.as_of)
  return AnswerQuestionResult(
      status="answered",
      answer=draft.answer,
      sources=[],
      missing_aspects=[],
      retrieval=AnswerRetrievalSummary(
          planned_mode="none", unmet_requirements=[]))
  ※ blank 回答は DirectAnswerDraft 構築時の ValidationError として工程側で
    顕在化する (service は valid な draft しか受け取らない)。

evidence 経路:
  3. outcome  = retriever.retrieve(plan, as_of=input.as_of)
  4. evidence = normalize_answer_evidence(outcome)
  5. 決定的前段:
       evidence が空
         -> synthesizer を呼ばず insufficient result を組み立てて返す
            answer  = 定型文
            sources = []
            missing = retrieval-empty 定型 -> unmet 定型
                      -> task_reports.missing の順で連結 (必ず 1 件以上)
  6. draft = synthesizer.synthesize(
       question=input.question, evidence=evidence,
       as_of=input.as_of,
       target_time_window=_plan_target_time_window(plan))
  7. citation 照合 (不正なら AnswerDraftInvalidError):
       - cited_refs に evidence に存在しない ref がある
       (answered → 引用必須 / insufficient → 理由必須は AnswerDraft の
        validator が構築時に保証済み)
  8. 組み立て (決定的):
       sources  = 引用された item の .source (採番順、cited_refs の重複は一意化)
       status   = "insufficient" if (unmet_requirements 非空
                   or draft.sufficiency=="insufficient") else "answered"
       missing  = status=="insufficient" のとき
                  unmet 定型句
                  -> task_reports.missing (task_index 昇順に整列して flatten)
                  -> draft.missing_aspects
                  の順で連結、完全一致 dedup (先勝ち)。answered のときは空。
       answer   = draft.answer (決定的前段のみ定型文)
       retrieval = AnswerRetrievalSummary(
                     planned_mode=plan.retrieval_mode,
                     unmet_requirements=outcome.unmet_requirements)
```

status 規則の補足: `unmet_requirements` 非空 → 常に insufficient は
contract の validator (answered は unmet を持てない) から導かれる規則。
計画した取得が満たせなかった場合、draft が answered を申告しても
「完全回答」を名乗らず、draft.answer を部分回答として insufficient で返す。

missing 下限の補足: insufficient になる 3 経路すべてで missing ≥1 が
保証される (決定的前段 = retrieval-empty 定型 / unmet 経路 = unmet 定型 /
自己申告経路 = draft validator の insufficient → missing 非空)。dedup は
先勝ちで要素を消すだけなので非空性を壊さない。contract の backstop
validator はこの保証の破れを構築時に検出する。

## Slice B への申し送り

本 slice で決めた契約を前提に、Slice B は以下を工程内に実装する
(本 slice では実装しない)。

- **synthesis 工程は planner 鏡写しの二層構造**: LLM 生出力 (lenient な
  raw draft、`QuestionPlanDraft` 相当) → 決定的補完 → strict な
  `AnswerDraft`。補完可能な欠陥 (insufficient + missing 空) は定型
  missing を補完し、defect (例: `insufficient_without_missing`) を
  audit / metrics に記録して**処理を止めない** (回答文=部分回答を失わない)。
  補完不能な欠陥 (unparseable / 存在しない ref の引用など) は retry →
  fallback。defect 分類 enum は値だけで原因が読める自己記述形式
  (既存 feedback 準拠)。
- **direct 工程も同形**: `DirectAnswerDraft` 構築時検証 + retry 巻き。
- **プロンプトは経路別に 2 本** (接地強制 / 自然回答)。
- planner 誤ルーティング (direct なのに答えられない類) の観測は
  audit / eval で行う。
- 表示ラベルが必要になったら consumer 側 (API schema 層 / emit 時) で
  sources と planned_mode から導出する。contract に整形済みラベルを
  再導入しない。

## Invariants

- `answer()` は経路選択の match 1 箇所と工程の順序のみを所有する。
  それ以外の variant 分岐 (`_is_direct_plan` のような bool helper) を
  持たない。variant 読み取りは `_plan_target_time_window(RetrievalPlan)`
  のみ。
- direct 経路は retriever / normalize / synthesizer に一切触れない。
  evidence 経路は direct_answerer に触れない。
- 「direct 回答は引用を持たない」は型で保証する (`DirectAnswerer` は
  evidence を受け取らない)。runtime 検証を追加しない。
- 回答経路の判別子は `retrieval.planned_mode == "none"` である
  (route のような派生ラベルを contract に持たない)。
- retriever port は `RetrievalPlan` のみ受ける。`NoRetrievalPlan` の
  no-op 経路は存在しない。
- `EvidenceAnswerSynthesizer` は非空 evidence でのみ呼ばれる (空なら
  決定的前段で insufficient を返す)。
- draft 契約は構築時に成立する: answered → cited_refs 非空・missing 空 /
  insufficient → missing 非空 / answer・missing 要素は non-blank。
  insufficient の自己申告は覆さない。
- final result 契約も構築時に成立する: answer・missing_aspects 要素は
  non-blank。service 経由の通常経路では draft 契約で保証されるが、
  `AnswerQuestionResult` でも backstop として保証する。
- insufficient result は必ず 1 件以上の missing_aspects を持つ
  (service の 3 経路すべてで保証 + contract backstop validator)。
- `as_of` は `input.as_of` を素通しし、再生成しない。
- `unmet_requirements` 非空の result は必ず insufficient。
- 本 slice では不正 draft を黙って修復せず raise して伝播させる
  (`AnswerDraftInvalidError` / ValidationError)。補完 + 記録で処理を
  止めない機構は Slice B が工程内に実装する。
- sources は引用された item の `.source` のみ。source_ref の再採番・
  provenance の再構築をしない。
- missing_aspects は最終不足理由のみ。`task_reports.missing` は
  task_index 昇順に整列してから flatten する。
- 各工程 (planner / retriever / synthesizer / direct_answerer) の想定外
  例外は握りつぶさず伝播する。
- `contract.py` の変更は New Types 節の列挙 (execution 系削除 +
  validator 書き換え/追加) が全て。それ以外の shape は変更しない。
- 両経路の result 組み立てを共通 helper に統合しない。

## Non-goals

- 実 LLM adapter ×2・プロンプト作成・raw draft 補完・defect audit /
  metrics・retry / fallback (Slice B。「Slice B への申し送り」参照)。
- replanning / escalation (direct で答えられない場合に planner へ差し戻す
  機構)。誤ルーティングは Slice B の audit / eval で観測する。
- 表示ラベルの設計 (API slice で consumer 側に導出を実装する)。
- probe script の `answer()` 延長 (Slice B の貫通で行う)。
- API endpoint / FastAPI DI / frontend 型生成。
- progress event / timeline (途中経過の可視化) の設計。
- synthesizer への不足ヒント (task_reports.missing 等) の供給。
- `plan_question` compatibility helper の整理。
- 定型文言の作り込み (規則のみ仕様化し、文言は実装で確定)。
- plan variant の追加・改名 (`RetrievalPlan` は既存 union の
  再グルーピングであり新 variant ではない)。

## Changed Files (rev.1 実装からの delta)

```text
backend/app/agent/contract.py                   (ExecutionRoute /
                                                 AnswerExecutionSummary /
                                                 execution field 削除、
                                                 NonBlankText 追加、
                                                 validator 書き換え + 追加)
backend/app/agent/__init__.py                   (export 追従 + RetrievalPlan)
backend/app/agent/planning/contract.py          (RetrievalPlan alias 追加、
                                                 QuestionPlan 再構成、__all__)
backend/app/agent/planning/__init__.py          (RetrievalPlan export)
backend/app/agent/answering/retrieval.py        (retrieve を RetrievalPlan に
                                                 絞り NoRetrievalPlan case 削除)
backend/app/agent/answering/synthesis.py        (EvidenceAnswerSynthesizer に
                                                 rename、validator 強化)
backend/app/agent/answering/direct.py           (新規: DirectAnswerer +
                                                 DirectAnswerDraft)
backend/app/agent/answering/service.py          (経路 dispatch 化、
                                                 _is_direct_plan /
                                                 execution 組み立て削除)
backend/app/agent/answering/__init__.py         (export 追従)
backend/tests/agent/test_contract.py            (execution summary テスト削除、
                                                 validator テスト追加/書き換え)
backend/tests/agent/answering/test_retrieval.py (NoRetrievalPlan ケース削除)
backend/tests/agent/answering/test_service.py   (direct 経路再編、
                                                 route assertion を sources +
                                                 planned_mode に置換)
```

## Tests

fake planner / retriever / synthesizer / direct answerer で検証する。
期待値は fixture から導出し、status / sources は本仕様の規則から決める。
plan は variant constructor で直接構築する。

### dispatch / direct 経路 (test_service.py)

1. `NoRetrievalPlan` → direct_answerer が question / input.as_of で呼ばれ、
   retriever / synthesizer は呼ばれない。result: answered /
   answer=draft.answer / sources 空 / missing_aspects 空 /
   planned_mode="none" / unmet 空。
2. `RetrievalPlan` (3 variant 各 1 ケース) → direct_answerer は呼ばれない。
3. direct_answerer の例外が伝播する。

### evidence 経路 正常系 (status / sources)

4. internal answered: internal evidence 2 件を引用 → answered、sources が
   引用 item の `.source` と一致 (採番順・全て internal kind)、
   missing_aspects 空。
5. external answered: external のみ引用 → sources に external source のみ。
6. internal_and_external answered で両方引用 → sources に両 kind。
7. **引用由来意味論の要**: `InternalAndExternalPlan` で external evidence も
   存在するが internal のみ引用 → answered、sources に external が載らない
   (planned_mode は "internal_and_external" のまま = 計画と接地は独立)。

### insufficient 系

8. 決定的前段: `InternalRetrievalPlan` で hits ゼロ → synthesizer が
   呼ばれず insufficient (定型 answer、sources 空)、missing_aspects に
   retrieval-empty 定型が入り空でない。
9. unmet cap: `InternalAndExternalPlan` で external unmet、internal を
   引用した answered draft → 最終 status は insufficient、missing_aspects
   に unmet 定型句を含み、answer は draft.answer (部分回答)。
10. 自己申告採用: draft が insufficient + missing_aspects + 部分引用 →
    insufficient、引用分は sources に載り、draft の missing が含まれる。
11. missing 連結: unmet 定型 + task_reports.missing (task_index 昇順、
    report を逆順で与えて整列を検証) + draft.missing がこの順で連結され、
    完全一致の重複が除去される。

### 不正 draft / 型 (draft 構築時検証)

12. evidence に存在しない ref を引用 → `AnswerDraftInvalidError`。
13. cited_refs の重複 → sources は一意・採番順。
14. `AnswerDraft` validator: answered + cited_refs 空 → ValidationError。
15. `AnswerDraft` validator: answered + missing_aspects → ValidationError。
16. `AnswerDraft` validator: insufficient + missing_aspects 空 →
    ValidationError (P1)。
17. `AnswerDraft` / `DirectAnswerDraft`: answer が空白のみ ("   ", "\n") →
    ValidationError (P2)。`AnswerDraft.missing_aspects` の空白のみ要素も
    ValidationError。

### contract (test_contract.py)

18. insufficient + missing_aspects 空 → ValidationError (P1 backstop)。
19. answer が空白のみ / missing_aspects に空白のみ要素 → ValidationError
    (P2 backstop)。
20. answered ∧ planned_mode≠"none" ∧ sources 空 → ValidationError
    (route 参照から書き換えた validator)。
21. planned_mode="none" ∧ sources 非空 → ValidationError。
22. `AnswerExecutionSummary` / `ExecutionRoute` のテスト群を削除。

### 配線 / 伝播

23. 工程素通し: planner に input がそのまま渡り、retriever に plan と
    input.as_of、synthesizer に question / evidence / input.as_of が渡る。
    target_time_window は `InternalAndExternalPlan` では plan の値、
    `InternalRetrievalPlan` では None が渡る (helper の variant 分岐検証)。
24. planner / retriever / synthesizer の例外が伝播する。

### 型レベルで排除されるテストの削除

25. test_retrieval.py の `NoRetrievalPlan` → 空 outcome テストを削除
    (retriever は `RetrievalPlan` のみ受けるため代替テスト不要。
    dispatch の網羅性は `assert_never` と test 1〜2 が保証する)。

## Done

- `QuestionAnsweringService.answer(input)` が上記 Behavior を実装し、
  `QuestionAnsweringAgent` Protocol を満たす。
- 経路 dispatch が `answer()` の match 1 箇所のみにあり、
  `_is_direct_plan` が存在しない。
- `DirectAnswerer` + `DirectAnswerDraft` (direct.py) /
  `EvidenceAnswerSynthesizer` (synthesis.py) の port 分離と
  `RetrievalPlan` alias が入り、retriever に `NoRetrievalPlan` case がない。
- `AnswerDraft` の validator が answered → cited_refs 非空・missing 空 /
  insufficient → missing 非空 / non-blank を保証する。
- contract.py から execution 系が消え、validator が planned_mode ベース +
  insufficient / non-blank backstop になっている。
- fake で answer() の全経路 (direct / evidence 正常 / insufficient /
  不正 draft) が検証できる。
- 上記テストが green。既存 suite に regression なし。
- API endpoint / DB schema には変更を加えない。
