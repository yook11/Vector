# internal query cap の plan 構築時点への前倒し + planner 2 段 audit

作成: 2026-07-07
前提: [`question-plan-variant-types.md`](./question-plan-variant-types.md) の variant 型化が実装済みであること
（branch codex/question-plan-variant-types、レビュー完了。probe script の import 修正
`app.agent.internal_retrieval.contract` → `app.agent.internal_retrieval` package re-export を含めて commit 済みにしてから着手）
Status: Implemented

---

## 1. Problem

variant 型化（前 slice）の時点で、internal queries の dedup + 3 件 cap という invariant は
**実行側**（`build_internal_search_queries`）にあり、plan 型は正規化前の値を運んでいる。このため:

- `InternalRetrievalPlan.internal_queries` は「正規化途中の値」で、実際に検索実行される値（≤3、dedup 済み）と乖離する
- ディスパッチ層（answering）が正規化 helper の呼び出し判断を持ち、配線を超えたドメイン正規化の知識を負っている
- audit の `internal_query_count`（正規化前）と `external_query_count`（正規化後）の意味論が非対称

invariant の所在を plan 構築時点（ファクトリ）へ前倒しし、「completed plan を受け取った時点で
internal query は最大 3 件・非空が型で保証済み」にする。LLM が 4 件以上返しても validation error にせず factory で丸める。

同時に、丸めで失われる planner の生の挙動（過剰生成）の可視性を **2 段 audit** で保つ:

- draft 受領時点: LLM 出力の件数を `PlannerDraftReceivedEvent` に焼く（internal / external 両方）
- completed plan 作成後: cap 後件数を従来どおり `PlannerFinalEvent` に焼く

## 2. Evidence（確認済み事実）

- `MAX_INTERNAL_QUERIES = 3` は `internal_retrieval/query_embedding.py:22` が定義。
  `__all__`（同 :14）と `internal_retrieval/__init__.py` が re-export し、
  VO validator（:43）・builder（:69）・`tests/agent/internal_retrieval/test_query_embedding.py`（:9, :43）が参照
- `build_internal_search_queries()` の app 内呼び出し元は variant 化後、
  `answering/service.py` の 2 箇所（internal / internal_and_external の dispatch）のみ
- builder の正規化セマンティクス: strip → 空除去 → **casefold dedup（最初の出現の原表記を保持）** → 3 件 cap（先頭優先）。
  `["NVIDIA", "nvidia", "OpenAI"]` → `["NVIDIA", "OpenAI"]` がテストで固定されている（test_query_embedding.py:36）
- `InternalSearchQueries` VO は「cap 済みでなければ構築拒否」+ blank query 拒否 validator を持つ（迂回構築の fail-fast ガード）。
  ただし空 tuple は既存どおり許容し、embedder / service の no-op 契約を維持する
- `PlannerAuditRecorder`（planning/audit.py:153）は **Protocol のみで本番実装は app 内に存在しない**（grep 確認済み）。
  実装はテスト fake のみ → protocol へのメソッド追加は fake 更新で閉じる。本番 recorder は統合 slice で実装予定
- `PlannerOutcomeCode`（audit.py:25、StrEnum）は ATTEMPT_FAILED / PLAN_CREATED / FALLBACK_USED を持ち、
  `PlannerFinalEvent` は factory（plan_created / fallback）で outcome_code を焼く。
  `_record_final_event` は recorder 例外を握って planning flow を落とさない
- `_plan_query_counts`（planning/service.py）は plan から件数を数えるため、cap 前倒し後は**無変更で** ≤3 の新値を返す
- audit の `external_query_count` は既に正規化後件数 → 本変更で internal / external の意味論が揃う
- `pytest -m unit` は 3078 件を収集する（conftest の自動 marker 付与により「not integration」と同値。検証コマンドとして有効）
- **依存方向**: internal_retrieval → planning は新規の依存辺だが、planning は internal_retrieval を import しないため循環しない
  （planning/__init__ → service → agent.contract の既知経路にも internal_retrieval は現れない）

## 3. 設計

### 3.1 正規化の前倒し（`planning/contract.py`）

- `MAX_INTERNAL_QUERIES = 3` を planning/contract.py へ移す（external の `EXTERNAL_RESEARCH_TASK_LIMIT` と同じく
  cap 定数は plan contract が単一所有）
- `InternalRetrievalPlan.internal_queries` / `InternalAndExternalPlan.internal_queries` に
  `max_length=MAX_INTERNAL_QUERIES` を付ける
- `plan_from_draft()` の internal 正規化を **strip → 空除去 → casefold dedup（最初の出現の原表記を保持）→
  3 件 cap（先頭優先）→ 空なら `[fallback_query]`** に変更（実行側 builder と同一セマンティクス）
- LLM draft が 5 件返しても factory が 3 件に丸めて valid plan を返す（validation error にしない）
- direct construction で 4 件以上は `ValidationError`（型 = 丸め済みの保証書、発火するのはファクトリ迂回 = 開発者バグのみ）
- `plan_from_draft()` 経由の completed plan は casefold dedup 済みとして扱う
- dedup は正規化セマンティクスであり、型単体には焼かない（型が保証するのは ≤3・非空・strip のみ）。
  直接構築は重複を型では拒否しない

### 3.2 実行側の簡素化（`internal_retrieval/query_embedding.py`）

- `MAX_INTERNAL_QUERIES` は planning.contract から import（定義削除）
- `build_internal_search_queries()` は app 内 caller が消えるため**削除**。
  併せて `query_embedding.py` の `__all__` と **`internal_retrieval/__init__.py` の import / `__all__` から除去**
  （残すと関数削除により import 時に即死する）
- `internal_retrieval/__init__.py` の `MAX_INTERNAL_QUERIES` re-export は公開名維持のため残す
- `InternalSearchQueries` VO とその validator は残す（ファクトリ迂回時の fail-fast ガード）。
  VO は `> MAX_INTERNAL_QUERIES` と blank query を拒否するが、空 tuple は既存 no-op 契約として許容する

### 3.3 ディスパッチ層（`answering/service.py`）

- internal path の `build_internal_search_queries(plan.internal_queries)` をやめ、
  `InternalSearchQueries(queries=tuple(plan.internal_queries))` を直接構築して葉へ渡す
  （plan の型保証 = max_length + PlanQuery 非空 strip により常に成功）
- これで正規化知識がディスパッチ層から消え、純粋な配線になる。葉サービスは引き続き plan を受け取らない

### 3.4 2 段 audit（`planning/audit.py` / `planning/service.py`）

- `PlannerOutcomeCode.DRAFT_RECEIVED = "question_plan_draft_received"` を追加
- `PlannerDraftReceivedEvent` を追加。fields:
  `attempt_number` / `retrieval_mode` / `draft_internal_query_count` / `draft_external_query_count` /
  `ai_model` / `prompt_version`（+ outcome_code 既定 DRAFT_RECEIVED）。**query text は焼かない**（PII / 膨張回避）
- draft 件数は raw draft list の長さをそのまま数える:
  `draft_internal_query_count = len(draft.internal_queries)` /
  `draft_external_query_count = len(draft.external_collection_goals)`。
  blank / duplicate / 4 件以上も正規化前の観測値として数える
- `PlannerAuditRecorder` protocol に `record_draft_received(event)` を追加
- 記録タイミング: **successful draft を受け取った直後、`plan_from_draft()` の前**
  （factory が万一失敗しても draft 受領の痕跡が残る）。初回成功は attempt_number=1、retry 成功は 2。
  parse に失敗した draft は event なし（従来どおり attempt failure が記録される）
- fallback path は draft event なし。final event は fallback plan 件数のまま
- recorder 例外は既存の final / attempt と同じく握って planning flow を落とさない
- `PlannerFinalEvent.internal_query_count` は cap 後件数（≤3）になる（コードは `_plan_query_counts` 無変更、
  plan の中身が変わることで自然に新値になる）

## 4. Invariants

1. LLM 境界は無変更: `QuestionPlanDraft` / Gemini response schema / planner prompt に手を入れない
   （schema description の "at most 3" とコードが一致するだけ）
2. external 側の正規化・cap・audit 意味論は完全に無変更
3. 正規化（strip / dedup / cap）の実施箇所はファクトリ 1 箇所。実行側・ディスパッチ層に再正規化を残さない
4. `plan_from_draft()` 経由の completed plan では、casefold dedup の「最初の出現の原表記を保持」セマンティクスを維持し、テストで固定する
5. completed plan variant の型が保証するのは「≤3・非空・strip 済み」。超過は境界（ファクトリ）で丸めて処理を止めない。
   `ValidationError` が出るのはファクトリ迂回の直接構築（= 開発者バグ）のみ
6. `InternalSearchQueries` VO は空 tuple を許容する既存 no-op 契約を維持し、`>3` と blank query のみ拒否する
7. audit に query text を焼かない
8. draft event の件数は raw draft list の `len(...)` とし、blank / duplicate / cap 超過を含める
9. recorder 失敗で planning flow を落とさない（draft event も同様）
10. DB schema / API response shape / frontend は変更しない

## 5. Non-goals

- 統合 slice（配線 + E2E）・回答生成 slice
- `PlannerAuditRecorder` の本番実装（protocol + test fake まで。統合 slice で 3 メソッドを実装する旨を申し送り）
- Gemini schema への `maxItems` 追加（description 依存のまま。structured output の挙動変更リスクを取らない）
- dedup の型レベル強制（正規化はファクトリ責務）
- external 側の draft 件数以外の可視化拡張

## 6. 実装ステップ

1. `planning/contract.py`: `MAX_INTERNAL_QUERIES` 移設 + variant への `max_length` + factory 正規化変更
2. `internal_retrieval/query_embedding.py`: 定数 import 化、`build_internal_search_queries` 削除、`__all__` 更新
3. `internal_retrieval/__init__.py`: builder の import / `__all__` 除去、`MAX_INTERNAL_QUERIES` re-export 維持
4. `answering/service.py`: VO 直接構築へ変更（2 箇所）、builder import 削除
5. `planning/audit.py`: outcome code / `PlannerDraftReceivedEvent` / protocol メソッド追加
6. `planning/service.py`: draft event 記録（成功 2 経路）、recorder 例外の握り
7. テスト再編（§7）
8. 最終 grep: `build_internal_search_queries` が app / tests / scripts に残っていないこと
9. 検証（§8）

## 7. テスト

| ファイル | 方針 |
|---|---|
| `tests/agent/planning/test_contract.py` | factory 正規化の新テスト: blank 除去 / **casefold dedup が最初の出現の原表記を保持**（test_query_embedding.py:36 から移設）/ 3 件 cap / 順序維持 / draft 5 件でも error にしない。direct construction 4 件は ValidationError。`internal_and_external` で internal / external 両 cap が効くこと。既存 `test_internal_queries_drop_blanks_and_keep_order_without_dedup_or_cap` は削除し dedup/cap ありの新挙動テストに置換 |
| `tests/agent/internal_retrieval/test_query_embedding.py` | builder テスト群は削除（factory 側へ移設）。VO の cap 拒否・blank query 拒否・空 tuple 許容テスト（既存 no-op 契約）は残す / 追加する。`MAX_INTERNAL_QUERIES` の import 元を張り替え |
| `tests/agent/planning/test_planner.py` | draft event が plan construction 前の raw 件数（`len(draft.internal_queries)` / `len(draft.external_collection_goals)`。blank / duplicate / cap 超過を含む。例: 5）を記録し、final event の `internal_query_count` が cap 後（3）になること（既存の中立パステストを新セマンティクスへ書き換え）。retry 成功時は draft event の attempt_number=2。draft / final どちらにも query text が入らないこと。recorder 失敗時に planning flow が落ちないこと。fallback path で draft event が無いこと |
| `tests/agent/answering/test_service.py` | dispatcher が plan の queries を `InternalSearchQueries` として葉へ渡すこと（`test_retrieve_internal_normalizes_queries_before_leaf_search` は「正規化済み plan をそのまま VO 化して渡す」テストに書き換え。正規化自体の検証は factory 側が正本） |

テスト設計・追加は test-writer agent に委譲する（プロジェクト規約）。
不変条件の正本: 正規化 = planning/test_contract.py、cap の型保証 = 同、迂回ガード = test_query_embedding.py（VO）、
audit 2 段 = test_planner.py。

## 8. Done（停止条件）

- [ ] `plan_from_draft` 経由の internal queries が常に ≤3・casefold dedup 済み（テストで固定）
- [ ] direct construction 4 件以上が ValidationError
- [ ] `build_internal_search_queries` が tree 全体（app / tests / scripts）から消えている
- [ ] ディスパッチ層に正規化呼び出しが残っていない（VO 直接構築のみ）
- [ ] draft event（raw 件数のみ・text なし）と final event（cap 後件数）の 2 段が audit テストで固定
- [ ] [`question-plan-variant-types.md`](./question-plan-variant-types.md) の §9-1 を「採用済み（本 spec へ）」に改訂済み
- [ ] 検証: `uv run ruff check app/` / `uv run ruff format --check app/` / `uv run pytest -m unit -q` /
      `make test-integration` / `uv run python scripts/probe_question_answering.py --help` +
      `uv run ruff check scripts/probe_question_answering.py`
- [ ] 上記を満たしたら停止。周辺改善は提案に留める

## 9. 決定の記録

- **`internal_query_count` の意味変更**（正規化前 → completed plan の件数 ≤3）は意図した変更。
  external と意味論が揃い、「plan の件数 = 実行される件数」になる
- **draft 件数の別 event 化**は前 spec §9-1 の「別 attribute は追加しない」からの意図的な転換。
  cap 前倒しにより planner の過剰生成が観測不能になるため、同 spec が示した将来案
  「draft 側の計数として internal / external 両方に同時導入」を採用した。
  final event への nullable attribute 追加ではなく別 event にしたのは、fallback path（draft なし）の
  歪みを避け、記録タイミング（factory 前）を素直に表現するため
- **draft event の件数は raw list の件数**。blank / duplicate / cap 超過を含むことで、LLM の過剰生成や空要素生成の観測可能性を保つ。
  completed plan の正規化後件数とは別の意味として扱う
