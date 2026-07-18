# QuestionPlan のプランごと variant 型化 — planner 出力の構造保証

作成: 2026-07-06
更新: 2026-07-06 レビュー第 1 回(6 件)・第 2 回(5 件)・第 3 回(3 件)を反映
ブランチ: codex/question-plan-variant-types
Status: **Implemented（2026-07-07）**。§9-1 は本 slice では非採用で実装後、
後続 slice [`internal-query-cap-and-planner-draft-audit.md`](./internal-query-cap-and-planner-draft-audit.md) で採用済み（同 spec が正本）

---

## 1. Problem

Q&A エージェントの `QuestionPlan` は単一型で、`retrieval_mode`（4 モード）と
`internal_queries` / `external_research_tasks` の相関を `model_validator` のランタイムチェックで守っている。
このため:

- 「external plan なのに internal_queries が入っている」等の不正状態が **型としては表現可能** で、validator が事後に弾いている
- 下流の InternalSearchService / ExternalSearchService が plan 丸ごとを受け取り、
  「自分に関係ないモードなら空を返す」防御フィルタを持つ。呼び出し側のバグが **空結果として静かに握り潰される**
- plan 型が `app/agent/contract.py`（agent 全体の雑多な境界型置き場）にあり、生産者（planning）が所有していない

これをプランごとの variant 型（union）に分割し、生産者側 = planning に定義を移す。
「この plan なら何が必要か」を型で読める構造にする。

## 2. Evidence（現状の確認済み事実）

- `QuestionPlan` 定義: `backend/app/agent/contract.py:65`（frozen、`RetrievalMode = Literal["none", "internal", "external", "internal_and_external"]`、相関 validator は 138-164 行）
- LLM 境界は分離済み: planner LLM（Gemini）は `QuestionPlanDraft`（`planning/plan_draft.py`、フラット型）を返し、
  Gemini に渡す schema は手書き（`planning/ai/schema_tool.py`）。**ドメイン型を union 化しても LLM の structured output は影響を受けない**
- ファクトリの継ぎ目が既にある: `QuestionPlan.from_draft()`（contract.py:76）と `safe_fallback()`（contract.py:127）。
  呼び出し元は `planning/service.py` のみ
- **plan 構築時の正規化は internal / external で非対称**:
  - `internal_queries`: strip + 空文字除去のみ（`_clean_plan_queries`、contract.py:167）。dedup・件数上限なし。空なら fallback_query 代入
  - `external_research_tasks`: strip + 空除去 + goal 重複除去（exact match）+ 3 件切り詰め（`_clean_external_research_tasks`、contract.py:175）。空なら fallback task 代入
  - Gemini schema（schema_tool.py）は両 list とも description で「Return at most 3 items」を頼むのみで `maxItems` の構造強制はない
- **ただし internal の dedup / cap は実行側に存在する**: `build_internal_search_queries()`
  （`internal_retrieval/query_embedding.py:52`）が strip + casefold dedup + `MAX_INTERNAL_QUERIES`(3) cap を行い、
  `InternalSearchService.embed_plan_queries` が plan.internal_queries を必ずこれに通す（`internal_retrieval/service.py:61`）。
  `InternalSearchQueries` VO は「cap 済みでなければ構築拒否」の validator を持つ。
  つまり計算資源の invariant は守られているが、**その所在が plan 構築時点ではなく実行時点**にあり、
  plan 型と audit の件数（下記）は正規化前の値を映す
- `MAX_INTERNAL_QUERIES` は `query_embedding.py:22` が定義・`__all__` export し、
  `internal_retrieval/__init__.py` が re-export、VO validator（:43）・builder（:69）・
  `tests/agent/internal_retrieval/test_query_embedding.py`（:9, :43）が参照する。
  `build_internal_search_queries` の app 内呼び出し元は `internal_retrieval/service.py:61` のみ
- 消費側の分岐は 4 箇所:
  - `answering/service.py:78` — `match plan.retrieval_mode`（retrieval ディスパッチの主分岐）
  - `internal_retrieval/service.py:58` — `if plan.retrieval_mode not in {"internal", "internal_and_external"}` 防御フィルタ
  - `external_search/service.py:35` — `if plan.retrieval_mode not in {"external", "internal_and_external"}` 防御フィルタ
  - `planning/service.py:252` — `external_unavailable_result()` が不正モードで `ValueError` を raise。
    **拒否挙動はテストで固定されている**（`tests/agent/planning/test_planner.py:433` `test_rejects_non_external_plan`）
- `answering/service.py:22` の **Protocol（`InternalArticleRetriever` / `ExternalPlanSearcher`）は plan 丸ごとを受ける契約**。
  葉サービスの署名変更はこの Protocol と test 側 fake に連動する
- metrics（`planning/metrics.py`）/ audit（`planning/audit.py`）の event / attribute 定義は `retrieval_mode` を値として持つのみ。
  ただし **`planning/service.py:194` の `_record_plan_created` が `len(plan.internal_queries)` / `len(plan.external_research_tasks)` を直読み**しており、
  variant 化後は全 variant が両属性を持たないため件数取得の明示分岐が必要（§3.4）。
  なお **`external_query_count` は正規化後件数**（dedup + 切り詰め後の plan を len）を既に記録しており、
  `internal_query_count`（正規化前）と意味論が非対称
- `target_time_window` の消費者は external_search 系のみ（service → runner → prompts / deepseek）。internal 側は不使用
- `QuestionPlan` の import 元（app 内）: answering / internal_retrieval / external_search の service に加え、
  **`planning/planner.py:7`**（QuestionPlanner protocol）と `planning/service.py`
- `ExternalResearchTask` / `EXTERNAL_RESEARCH_TASK_LIMIT` の旧 import は app 側 4 ファイルに加え、
  **tests 側 9 ファイル**（test_contract / answering 2 / external_search 4 / planning 1 / internal_retrieval 1）と
  **`backend/scripts/probe_question_answering.py`** に残る。sweep はこれら全てが対象
- planning は answering / internal_retrieval / external_search を import していない → planning へ移しても循環なし。
  ただし **`planning/__init__.py` は `planning.service` を import しており、service は `app.agent.contract` を import する**。
  agent/contract.py から planning 配下を runtime import すると循環になる（§3.1 の RetrievalMode 所在判断の根拠）
- probe script は `QuestionPlan(retrieval_mode="external", ...)` を直接構築（`scripts/probe_question_answering.py:133`）し、
  internal search stub の署名も plan を受ける
- Synthesizer は plan を知らない: #883 の `normalize_answer_evidence`（`answering/evidence.py`）で `AnswerEvidenceItem` に正規化済み。回答側は今回のスコープ外
- テスト: `tests/agent/test_contract.py` に plan 関連 41 件（うち相関 validator 10 件、from_draft 正規化 6 件。
  internal queries は「strip + 空除去 + 順序維持」だけを固定しており dedup / 上限のテストは存在しない）、
  消費側は tests/agent/{answering,internal_retrieval,external_search} に mode フィルタテスト含め 33 件
- backend の `/check` は ruff check + ruff format --check + pytest のみ。**mypy / pyright は含まれない**
  （→ 署名を型で絞っても実行時流入は防げないため、runtime guard の要否を §3.4 で明示する）

## 3. 設計

### 3.1 型定義（新設: `backend/app/agent/planning/contract.py`）

external_search が subpackage 直下に `contract.py` を持つ既存規約に合わせる。

```python
PlanQuery = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ExternalResearchTask(BaseModel):
    # 現行定義をそのまま移設（frozen, str_strip_whitespace, collection_goal min_length=1）
    ...


class NoRetrievalPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    retrieval_mode: Literal["none"] = "none"
    reason: str = Field(min_length=1)


class InternalRetrievalPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    retrieval_mode: Literal["internal"] = "internal"
    internal_queries: list[PlanQuery] = Field(min_length=1)  # max_length は §9-1 の採否に連動
    reason: str = Field(min_length=1)


class ExternalSearchPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    retrieval_mode: Literal["external"] = "external"
    external_research_tasks: list[ExternalResearchTask] = Field(
        min_length=1, max_length=EXTERNAL_RESEARCH_TASK_LIMIT
    )
    target_time_window: str | None = None
    reason: str = Field(min_length=1)
    # model_validator: collection_goal の重複禁止（これだけは field constraint で表せない）


class InternalAndExternalPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    retrieval_mode: Literal["internal_and_external"] = "internal_and_external"
    internal_queries: list[PlanQuery] = Field(min_length=1)
    external_research_tasks: list[ExternalResearchTask] = Field(
        min_length=1, max_length=EXTERNAL_RESEARCH_TASK_LIMIT
    )
    target_time_window: str | None = None
    reason: str = Field(min_length=1)
    # model_validator: goal 重複禁止（ExternalSearchPlan と同一。共有は validator 関数の再利用に留め、基底クラスは作らない）


type QuestionPlan = (
    NoRetrievalPlan | InternalRetrievalPlan | ExternalSearchPlan | InternalAndExternalPlan
)
```

設計判断:

- **相関 validator は消滅**。「モードとフィールドの組み合わせ」は型で表現不能になる。残るランタイム検証は
  field constraint（非空・件数上限・strip）と goal 重複チェックのみ
- **全 variant に `extra="forbid"`**。Pydantic v2 の既定（extra="ignore"）では
  `InternalRetrievalPlan(..., external_research_tasks=[...])` が構築でき余分な値が静かに捨てられる。
  「構築不能」の主張を成立させるために必須
- **基底クラスは作らない**。共通なのは `reason` と frozen 設定だけで、重複排除目的の抽象化はしない（Scope Rules）
- **discriminator フィールド（`retrieval_mode: Literal[...]`）を各 variant に残す**。
  metrics / audit が `plan.retrieval_mode` を値として記録しており、attribute 互換を無変更で維持するため。
  union を Pydantic で validate する箇所は現状ないため `Annotated + Field(discriminator=...)` は不要（必要になったら足す）
- **`RetrievalMode` alias は `agent/contract.py` に残す**。理由:
  (1) `AnswerRetrievalSummary.planned_mode`（agent 外向き契約、contract.py:203）の語彙として引き続き必要。
  (2) agent/contract.py から planning 配下を runtime import すると `planning/__init__.py` → `planning.service` →
  `app.agent.contract`（初期化途中）の循環になる。
  副次効果として `plan_draft.py` / `metrics.py` / `audit.py` / `ai/schema_tool.py` の RetrievalMode import は現行のまま無変更で済む。
  variant の discriminator はインライン Literal なので planning/contract.py は RetrievalMode を必要としない
- **`target_time_window` は external 系 2 variant のみ保持**。消費者が external_search のみであることを確認済み。
  `none` / `internal` の draft に time window が入っていても捨てる（現状も dead data）

### 3.2 ファクトリ（同じく `planning/contract.py`）

union の型 alias に classmethod は置けないため、module-level 関数にする。

```python
def plan_from_draft(draft: QuestionPlanDraft, *, fallback_query: str) -> QuestionPlan: ...
def safe_fallback_plan(*, fallback_query: str) -> InternalRetrievalPlan: ...
```

- 現行の正規化ロジックを **現行挙動どおり** 移植する:
  - internal_queries: strip + 空文字除去 + 順序維持。空なら `[fallback_query]`
  - external_research_tasks: strip + 空除去 + goal 重複除去 + 3 件切り詰め。空なら fallback task
  - internal 側への dedup / 件数クランプの **plan 構築時点への前倒し**（現在は実行側 `build_internal_search_queries` が担う）は
    §9-1 で採否を決める（採用時の変更一式は §9-1 に集約）
- `QuestionPlanDraft` の import は TYPE_CHECKING のみ（現行 contract.py と同じ手法。plan_draft.py は
  `RetrievalMode` を agent/contract.py から import し続けるため、planning/contract.py との間に runtime 依存は発生しない）

### 3.3 依存方向

```
agent/contract.py（外向き契約 + RetrievalMode 語彙）
  ↑ planning/contract.py（plan variant の正本。agent/contract への runtime import なし）
      ↑ planning/*（planner.py の protocol 署名、service のファクトリ呼び出し。draft / metrics / audit / schema_tool は無変更）
      ↑ answering/service.py（union を受けて match ディスパッチ）
      ↑ external_search（ExternalResearchTask を import）
```

下流工程 → planning（生産者）の一方向。agent 直下 `contract.py` には
`AnswerQuestionInput` / `AnswerQuestionResult` / `AnswerRetrievalSummary` / source 型 / `RetrievalMode` /
`QuestionAnsweringAgent` protocol 等の agent 外向き契約が残る。
葉サービスは union も variant も受け取らない（internal は既存 VO `InternalSearchQueries`、external はフィールド。§3.4）。

### 3.4 消費側の変更

| 箇所 | 現状 | 変更後 |
|---|---|---|
| `answering/service.py:78` `retrieve()` | `match plan.retrieval_mode`（文字列） | `match plan:` で variant に分岐 + `assert_never` で exhaustiveness を保証。**型分岐を知る唯一のディスパッチ層**。internal 系 variant では `plan.internal_queries` を `InternalSearchQueries` に変換して葉へ渡す（§9-1 非採用時は `build_internal_search_queries()` 経由、採用時は VO 直接構築） |
| `answering/service.py:22` Protocol 2 種 | `InternalArticleRetriever` / `ExternalPlanSearcher` が plan 丸ごとを受ける契約 | 葉の新署名に合わせて Protocol を更新。test 側 fake / probe stub も連動して書き換え |
| `internal_retrieval/service.py` | plan 丸ごと受領 + mode 防御フィルタ + 内部で `build_internal_search_queries` | **既存 VO `InternalSearchQueries` を受ける署名**に絞り、フィルタと内部正規化を削除。生 `list[str]` の暗黙信頼を作らず、operation の前提条件（正規化・cap 済み）を型で受ける（typed-pipeline-preconditions の precondition 型と同型。VO は既存で新規抽象化ではない）。VO の cap validator が迂回構築への fail fast ガードを兼ねる |
| `external_search/service.py` | plan 丸ごと受領 + mode 防御フィルタ | `external_research_tasks` + `target_time_window` を受ける署名に絞り、フィルタ削除（tasks は要素型 `ExternalResearchTask` が正規化済みを保証するためフィールド渡しで足りる。専用 VO は作らない） |
| `planning/service.py:252` `external_unavailable_result()` | 全 plan 受領 + 不正モードで `ValueError` | 署名を `ExternalSearchPlan \| InternalAndExternalPlan` に絞る。**runtime guard は残す**: /check に型チェッカーが無く実行時流入を型では防げないため、メッセージ分岐自体を `match plan:` で書き default を `assert_never` にする（ValueError は削除、非 external variant の流入は AssertionError で停止）。既存の拒否テスト（test_planner.py:433）は新しい失敗様式に書き換えて維持 |
| `planning/service.py:194` `_record_plan_created` | `len(plan.internal_queries)` / `len(plan.external_research_tasks)` を直読み | planning/service.py 内に stage-local helper `_plan_query_counts(plan) -> tuple[int, int]`（variant match、非該当は 0）を置き、`PlannerFinalEvent` の attribute（internal_query_count / external_query_count）は不変に保つ（§9-1 採用時の internal_query_count の意味変更は §9-1 参照）。整形 helper を domain 型に持たせず consumer 側に置く既存方針に従う |
| `planning/service.py:228` ほか | `QuestionPlan.safe_fallback()` / `QuestionPlan.from_draft()` | `safe_fallback_plan()` / `plan_from_draft()` へ張り替え |
| `planning/planner.py:7` | `QuestionPlan` を agent/contract から import（protocol 署名） | import 元を planning/contract へ張り替え |
| `planning/metrics.py` / `audit.py` / `plan_draft.py` / `ai/schema_tool.py` | `RetrievalMode` を agent/contract から import | **無変更**（RetrievalMode は agent/contract に残るため） |
| `app/agent/__init__.py` | contract から re-export | `QuestionPlan` + variant 4 種の re-export 元を planning/contract に張り替え。`RetrievalMode` は agent/contract のまま。公開名は維持 |
| `backend/scripts/probe_question_answering.py` | `QuestionPlan(retrieval_mode="external", ...)` を直接構築（133 行）+ internal search stub が plan 受領 | `ExternalSearchPlan` 構築へ変更。import 元と stub 署名（新 Protocol 準拠）も追随 |
| tests 側の旧 import（9 ファイル） | `ExternalResearchTask` 等を agent/contract から import | import 元のみ planning/contract へ張り替え（§7） |

「plan の型分岐を知ってよいのは retrieval ディスパッチ層（と生産者である planning 内部）まで」を不変条件とする。

## 4. Invariants（この変更で守り続けるもの）

1. LLM 境界は無変更: プランナーの prompt / Gemini response_schema / `QuestionPlanDraft` に一切手を入れない
2. `retrieval_mode` の値集合と文字列は不変。audit / metrics の attribute 名と `external_query_count` の値は不変。
   `internal_query_count` は §9-1 非採用時は不変、**採用時のみ「正規化後件数」への意図した意味変更**
   （external と同じ「plan が実行する件数」に揃う。新値はテストで固定）
3. retrieval の観測可能な挙動は不変: 各モードの検索実行・fallback plan・クエリ正規化は現行と同一
   （§9-1 を採用する場合の変化点は §9-1 に列挙し、テストで固定する）
4. 不正な組み合わせは validator で弾くのではなく **構築不能**（型 + field constraint + extra="forbid"）にする
5. 全 variant は frozen
6. 型分岐（variant への match / isinstance）は retrieval ディスパッチ層と planning 内部のみ。葉のサービス・synthesizer には漏らさない
7. 型で絞った署名への実行時流入（型チェッカー不在のため可能）は、silent skip ではなく fail fast（assert_never / VO validator）で顕在化させる

## 5. Non-goals

- 統合 slice（配線 + E2E）・回答生成 slice の実装（別作業。本リファクタはその前提整備）
- `retrieval_mode` 値のリネーム、audit / metrics の attribute 名変更
- `ExecutionRoute` / `AnswerQuestionResult` 等、agent 外向き契約の再設計
- API schema（app/schemas）・frontend 型への影響（agent 内部型のみ。/gen-types 不要）
- synthesizer / evidence 正規化（#883）への変更
- 基底クラス・共通 mixin の導入
- backend への型チェッカー（mypy / pyright）導入（有用だが新規 dependency のため別途提案・合意してから）

## 6. 実装ステップ

1. `planning/contract.py` 新設: `ExternalResearchTask` / `EXTERNAL_RESEARCH_TASK_LIMIT` /
   4 variant / `QuestionPlan` alias / `plan_from_draft` / `safe_fallback_plan` / 正規化ヘルパを移植
2. `agent/contract.py` から旧 `QuestionPlan`・`ExternalResearchTask`・正規化ヘルパ群を削除
   （`RetrievalMode` と `AnswerRetrievalSummary` 以下の外向き契約は残す）
3. import 張り替え（app 側）: `agent/__init__.py`、`planning/planner.py`、
   external_search 4 ファイル（contract / runner / ai/prompts / ai/deepseek）
4. `planning/service.py`: ファクトリ呼び出し張り替え + `external_unavailable_result` の署名絞り込み
   （match + assert_never の runtime guard 付き）+ `_plan_query_counts` helper 追加
5. `answering/service.py`: Protocol 2 種の署名更新、variant match + `assert_never` 化、
   internal 系は `InternalSearchQueries` への変換を挟んで葉へ、external 系はフィールド渡し
6. `internal_retrieval/service.py` / `external_search/service.py`: 署名絞り込み + 防御フィルタ削除
   （internal は `InternalSearchQueries` 受けに変更、内部の `build_internal_search_queries` 呼び出し削除）
7. §9-1 採用時のみ: ファクトリへの dedup/cap 前倒し一式（§9-1 の変更リストに従う）
8. `backend/scripts/probe_question_answering.py`: `ExternalSearchPlan` 構築 + import + stub 署名の追随
9. テスト再編（§7。tests 側 9 ファイルの import 張り替え含む）
10. 最終 grep 掃討（app / tests / scripts の tree 全体）: `retrieval_mode not in` / `QuestionPlan.from_draft` /
    `safe_fallback` / `QuestionPlan(` の直接構築 / **`from app.agent.contract import` 経由の
    `ExternalResearchTask`・`EXTERNAL_RESEARCH_TASK_LIMIT`・`QuestionPlan`** が残っていないこと
11. `/check`（backend ruff + pytest）

## 7. テスト再編

| ファイル | 方針 |
|---|---|
| `tests/agent/test_contract.py` | plan 関連を `tests/agent/planning/` へ移動。**相関 validator テスト 10 件は削除**し、代わりに各 variant の構築不能テスト（空 queries 拒否 / task 4 件拒否 / goal 重複拒否 / strip / **extra フィールド拒否**）へ置換。from_draft 6 件は `plan_from_draft` テストとして移植し、internal queries の「strip + 空除去 + 順序維持」の現行挙動固定を維持（§9-1 採用時は dedup / cap テストを factory 側へ新設） |
| `tests/agent/planning/test_planner.py` | `test_rejects_non_external_plan`（:433）を assert_never 由来の失敗様式（AssertionError）期待に書き換えて拒否保証を維持 |
| `tests/agent/internal_retrieval/test_service.py` | mode フィルタテスト（none=skip / external=skip）は対象コードごと削除。`InternalSearchQueries` を渡す新署名でテストを書き直し（正規化のテスト責務は builder / factory 側にあり、service テストから消える） |
| `tests/agent/internal_retrieval/test_query_embedding.py` | §9-1 非採用時: 無変更。採用時: §9-1 の定数移動に伴う import 張り替え + builder テストの factory 側への移設 |
| `tests/agent/external_search/test_service.py` | フィールドベースの新署名へ。fake は新 Protocol 準拠に更新 |
| `tests/agent/answering/test_service.py` | 4 モードケースを variant 構築に書き換え（アサーション不変）。「どのモードでどの検索が呼ばれるか」の責務担保はここに一元化される。fake は新 Protocol（`InternalSearchQueries` 受け）準拠に更新 |
| `tests/agent/planning/`（audit / service） | `_plan_query_counts` 経由でも `PlannerFinalEvent` の件数 attribute が現行と同値（§9-1 採用時は internal のみ意図した新値）になることを固定 |
| `tests/agent/answering/test_evidence.py` ほか旧 import を持つ計 9 ファイル | ロジック変更なし。`ExternalResearchTask` 等の import 元を planning/contract へ張り替えのみ |

テスト設計・追加は test-writer agent に委譲する（プロジェクト規約）。

## 8. Done（停止条件）

- [ ] plan 型の正本が `planning/contract.py` にあり、`agent/contract.py` に plan 関連定義が残っていない（RetrievalMode は意図して残置）
- [ ] 相関 `model_validator` が存在しない（不正組み合わせが構築不能。extra="forbid" 含む）
- [ ] 葉サービスの防御フィルタが削除され、`external_unavailable_result` は match + assert_never の fail fast に置換されている（拒否テスト維持）
- [ ] `match plan:` が `assert_never` で exhaustive（ランタイム防御として機能し、将来型チェッカーを導入すれば variant 追加漏れを静的検出できる形。現行 /check に型チェッカーは無いため静的検出は本変更の保証範囲外）
- [ ] metrics / audit の attribute 名・値が変更前と同一（§9-1 採用時のみ internal_query_count は §9-1 で定義した新値でテスト固定）
- [ ] probe script が新型で動作する（実行は Tavily/DeepSeek 実呼び出しのため構文・型レベル確認まで）
- [ ] 最終 grep 掃討（§6-10）が空振りする
- [ ] テスト再編完了、`/check` green
- [ ] 上記を満たしたら停止。周辺改善（他の contract 整理等）は提案に留める

## 9. 未決事項（決着済み）

1. **internal_queries の dedup + 3 件 cap の plan 構築時点への前倒し** —
   本 slice では**非採用**で実装したが、後続 sliceとして **2026-07-07 に採用済み**。
   正本は [`internal-query-cap-and-planner-draft-audit.md`](./internal-query-cap-and-planner-draft-audit.md)。
   正規化は `plan_from_draft()` へ前倒しし、dispatcher は `InternalSearchQueries` を直接構築する。
   当初本項が「別 attribute は追加しない」とした過剰生成の可視化は、同 spec で
   `PlannerDraftReceivedEvent`（draft 側の raw internal / external 件数を焼く 2 段 audit）として採用に転換した
2. **`none` variant の命名**: 本 spec は `NoRetrievalPlan` を採用（`retrieval_mode="none"` と一致し、
   「直接回答するか」は route 側の語彙で `ExecutionRoute` と混線するため）。当初案は `DirectPlan`
3. **葉サービスの新メソッド名**: `search_plan_articles(plan)` → `search_articles(queries)` 等の具体名は実装時に決定
