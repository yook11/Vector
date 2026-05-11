# Stage 4 Assessment 振る舞いリファクタ — 実装プラン

`specs/pipeline-events-stage4-assessment.md` (確定 spec) の実装ロードマップ。**案 B (6 PR 細粒度)** で段階的に切り替える。各 PR は独立 review / 独立 deploy 可能、中途半端な状態でも production の挙動が壊れない設計。

ステータス: **着手準備完了** (本 plan の merge 後 PR1 から順に着手)

---

## 全体像

### 依存グラフ

```
                        ┌──────────────────────────────┐
                        │ PR1 (a)                       │
                        │ Stage 4 ACL foundation        │
                        │ (tuple-based mapping + markers)│
                        └──┬─────────────────┬──────────┘
                           │                  │
                           ↓                  ↓
        ┌──────────────────────┐    ┌────────────────────────┐
        │ PR2 (b)(c)           │    │ PR4 (h)                 │
        │ Classifier 公開型整理 │    │ Alembic migration       │
        │ + envelope + parse   │    │ Stage rename + category │
        │ + Layer 2-B markers  │    │                          │
        └──────┬───────────────┘    └────────┬───────────────┘
               │                              │
               ↓                              ↓
   ┌───────────────────────────┐  ┌────────────────────────┐
   │ PR3 (d)                    │  │ PR5 (g)                 │
   │ Classifier 実装改修        │  │ AssessmentPayload +     │
   │ _translate_error / _call_once│  │ AssessmentAuditRepository│
   └───────┬───────────────────┘  └────────┬───────────────┘
           │                                │
           └────────────┬───────────────────┘
                        ↓
        ┌────────────────────────────────────────────┐
        │ PR6 (e)(f) — 最終切り替え                    │
        │ Service ACL boundary + 同 tx audit          │
        │ + Task 3 except dispatch + record_assess_fail│
        └────────────────────────────────────────────┘
```

### PR 一覧

| PR | 内容 | 規模目安 | 依存 |
|---|---|---|---|
| PR1 | Stage 4 ACL foundation (tuple-based markers + provider mapping) | 小 (~290 行) | なし |
| PR2 | Classifier 公開型整理 + envelope + parse + Layer 2-B markers | 中 (~280 行) | なし (PR1 と並行可、merge 順序自由) |
| PR3 | Classifier 実装改修 (`_translate_error` / `_call_once`) | 中 (~400 行) | PR1 + PR2 |
| PR4 | Alembic migration (Stage rename + category 追加) | 小 (~150 行) | なし (PR1〜3 と並行可) |
| PR5 | `AssessmentPayload` + `AssessmentAuditRepository` | 中 (~350 行) | PR1 + PR4 |
| PR6 | Service ACL + Task dispatch + `record_assessment_failure` | 中 (~400 行) | PR1 + PR2 + PR3 + PR5 |

### deploy 段階の挙動 (中途状態が production を壊さないか)

| 時点 | 本番挙動 |
|---|---|
| PR1 merge 後 | 新規ファイル (`assessment/errors.py` / `assessment/provider_mapping.py`) 増えるだけ、import されない → 挙動不変 |
| PR2 merge 後 | `AssessmentResponse` → `AssessmentResult` rename と内部詰め替え集約 + Layer 2-B markers (`AssessmentResponseInvalidError` / `AssessmentCategoryMissingError`) を `assessment/errors.py` に追加。Service の caller は型名変更のみ、挙動不変 |
| PR3 merge 後 | classifier の戻り値が envelope (`AssessmentCall`)、Service が envelope を unpack する経路に。**provider 例外は素通し** (まだ ACL なし)、既存挙動と同じ。失敗時は旧経路で audit 焼付 |
| PR4 merge 後 | DB CHECK 制約に `ASSESSMENT` / `non_retryable_keep_extraction` 追加。既存 row は `Stage.CLASSIFICATION` のまま稼働、新規書き込みも旧値で動作 |
| PR5 merge 後 | `AssessmentPayload` / `AssessmentAuditRepository` ファイルが import 可能になる。まだ書き込み経路に繋がっていない |
| **PR6 merge 後** | **本番挙動の最終切り替え**。Service が ACL boundary、Task が 2-marker dispatch、`pipeline_events` への audit 焼付が稼働 |

---

## PR1 — Stage 4 ACL foundation (tuple-based mapping + markers)

**目的**: tuple-based ACL の foundation を Stage 4 側に閉じて敷く。新規ファイルのみ、既存挙動への影響ゼロ。**provider 側 (`app/analysis/errors/`) は一切 touch しない**。

### スコープ

含む:
- `app/analysis/assessment/errors.py` 新規 — `AssessmentError` / `AssessmentRecoverableError` / `AssessmentTerminalSkipError` (Layer 1 marker、`code: str` + `provider_error: AIProviderError | None = None` の 2 instance attr)
- `app/analysis/assessment/provider_mapping.py` 新規 — `ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS` / `ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS` の 2 tuple + `map_provider_to_assessment(exc)` 関数 (`isinstance(exc, <tuple>)` で dispatch)

含まない (次 PR 送り):
- provider 側変更 (`AIProviderFailureKind` 等は本方針では永久に不要)
- Layer 2-B markers (`AssessmentResponseInvalidError` / `AssessmentCategoryMissingError`) — `parse_assessment` 実装と同時の方が自然なので **PR2 送り**
- `BaseClassifier._translate_error` の改修 (PR3)
- Service / Task / AuditRepository の改修 (PR5/PR6)

### 作業内訳

1. `app/analysis/assessment/errors.py` 新規:
   - `AssessmentError(Exception)` — Stage 4 全例外の共通基底
   - `AssessmentRecoverableError(AssessmentError)` — `code: str` + `provider_error: AIProviderError | None = None` の 2 instance attr、constructor は `(message="", *, code, provider_error=None)`
   - `AssessmentTerminalSkipError(AssessmentError)` — 同 signature
   - foundation marker (`RetryableError` 等) は **継承しない**
2. `app/analysis/assessment/provider_mapping.py` 新規:
   - `ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS: tuple[type[AIProviderError], ...]` — Network / ServiceUnavailable / RateLimited / QuotaExhausted の 4 種
   - `ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS: tuple[type[AIProviderError], ...]` — Configuration / RequestInvalid / InsufficientBalance / InputRejected / OutputBlocked の 5 種
   - `map_provider_to_assessment(exc)` — `isinstance(exc, <tuple>)` で dispatch、未登録は `TypeError`
3. spec 同梱更新 (本 PR で同時 merge):
   - `specs/pipeline-events-stage4-assessment.md` を tuple 方式に書き換え
   - 本 plan の §PR1 (= 本セクション) と全体像のスコープ表を更新

### テスト戦略

- `tests/analysis/assessment/test_errors.py`:
  - `AssessmentRecoverableError("msg", code="x", provider_error=AIProviderRateLimitedError("..."))` が `code` / `provider_error` instance attr に値を保持
  - `AssessmentTerminalSkipError` 同上
  - `provider_error` の **default が None** (PR2 の Layer 2-B で使う前提)
  - `code` がキーワード必須 (`AssessmentRecoverableError("msg")` は TypeError)
  - `issubclass(AssessmentRecoverableError, AssessmentError)` / `AssessmentTerminalSkipError, AssessmentError`
  - foundation marker 非継承: `not issubclass(AssessmentRecoverableError, RetryableError)`
- `tests/analysis/assessment/test_provider_mapping.py`:
  - tuple 内容の固定値テスト (parametrize で 9 種 provider error class の所属確認)
  - 網羅性 (`set(recoverable) | set(terminal_skip) == 9 種 concrete `AIProviderError` 集合`)
  - 排他性 (`set(...) & set(...) == set()`)
  - `map_provider_to_assessment` の dispatch (parametrize で 9 種 instance を投入)
  - 詰め替え後の `assessment_exc.provider_error is original` (identity 保持)
  - 詰め替え後の `assessment_exc.code == original.CODE`
  - 未登録 `AIProviderError` subclass で `TypeError`

### 不変条件

- **`backend/app/analysis/errors/` を一切 touch しない** (`git diff main -- backend/app/analysis/errors/` が空)
- Stage 3 の `extract_content` 経路は一切 touch しない
- `AIProvider*Error.CODE` 文字列を変えない (既存 `pipeline_events.code` 列の値継続性)
- 新規 import 経路ゼロ (classifier / service / task / repository を本 PR で touch しない → 既存挙動は完全に不変)

### review 観点

- `git diff main -- backend/app/analysis/errors/` が空 (provider 側 touch ゼロ)
- 9 種 concrete `AIProviderError` subclass がすべて 2 tuple のいずれかに登録 (網羅性) かつ重複なし (排他性)
- 9 種の振り分けが spec § Stage 4 marker 表と完全一致 (Network/ServiceUnavailable/RateLimited/QuotaExhausted = Recoverable、Configuration/RequestInvalid/InsufficientBalance/InputRejected/OutputBlocked = TerminalSkip)
- mapper 経由で詰め替えた marker の `provider_error is original_exc` (identity 保持) かつ `code == original_exc.CODE`
- 未登録 `AIProviderError` subclass で `TypeError`
- Stage 4 marker の foundation 非継承 (`not issubclass(AssessmentRecoverableError, RetryableError)`)
- `provider_error` の default が `None` (PR2 で Layer 2-B が provider_error なしで raise できる準備)
- 新規 import 経路ゼロ (classifier / service / task / repository が本 PR で touch されていない)
- spec 整合: 履歴に第 6 版追記、`AIProviderFailureKind` / `AssessmentRetryableError` / `inline_retry` の残骸が修正コードに無い (履歴・変更理由文脈の意図的な参照のみ許容)

---

## PR2 — Classifier 公開型整理 + envelope + parse + Layer 2-B markers

**目的**: AI 境界の型整理。`ClassificationRawResponse` 廃止、`AssessmentResult` rename、`InScopeCategory` 新設、parse 関数を `AssessmentCall` 構築まで集約。あわせて `parse_assessment` が raise する Layer 2-B markers (`AssessmentResponseInvalidError` / `AssessmentCategoryMissingError`) を `assessment/errors.py` に追加 (PR1 で導入した Stage 4 marker 構造を継承)。

### スコープ

含む:
- `app/analysis/classifier/schema.py` 改修
  - `ClassificationRawResponse` 削除
  - `InScopeCategory` 新設 (12 値、`OUT_OF_SCOPE` 除外)
  - `InScope.category` 型を `ValidCategory` → `InScopeCategory` に変更
  - `AssessmentResponse` → `AssessmentResult` rename (type alias)
- `app/analysis/classifier/envelope.py` 新規 — `AssessmentCall` dataclass
- `app/analysis/classifier/parse.py` 新規 — `parse_assessment(payload: dict) -> AssessmentResult`
- `app/analysis/assessment/errors.py` 追記 — `AssessmentResponseInvalidError(AssessmentRecoverableError)` / `AssessmentCategoryMissingError(AssessmentTerminalSkipError)` (Layer 2-B、`provider_error=None` で marker を直接継承、内部で `super().__init__(message, code="assessment_*")` を呼ぶ)
- 既存 caller の追従 (rename 反映、`InScopeCategory` 受け取り)
  - `app/analysis/assessment/service.py` (`InScope.category` 参照箇所)
  - `app/analysis/assessment/in_scope_repository.py` (DB 書き込み時の slug 取得)

含まない:
- `BaseClassifier._call_api` / `_translate_error` の改修 (PR3)
- Service.execute() の ACL boundary 化 (PR6)

### 作業内訳

1. `schema.py`:
   - `ClassificationRawResponse` クラス削除
   - `InScopeCategory(StrEnum)` 追加 (12 値)
   - `InScope.category: InScopeCategory` に型変更
   - `AssessmentResponse = InScope | OutOfScope` を `AssessmentResult` に rename + docstring 更新
2. `envelope.py` 新規:
   - `AssessmentCall` (frozen dataclass、5 field: result / raw_response / raw_category / raw_topic / prompt_version)
3. `parse.py` 新規:
   - `parse_assessment(payload)` 関数 (spec §Classifier 公開型 の skeleton 通り)
   - 例外 raise は `AssessmentResponseInvalidError` (PR1 で導入済)
4. caller 追従:
   - `service.py` で `in_scope.category.value` (slug) を扱う箇所を `InScopeCategory.value` で取得
   - 既存テストの `AssessmentResponse` import を `AssessmentResult` に更新
   - `ClassificationRawResponse` import / 利用箇所 (もしあれば) 削除

### テスト戦略

- `tests/analysis/classifier/test_schema.py`:
  - `InScopeCategory` 12 値、`OUT_OF_SCOPE` 不在
  - `InScope.category` が `InScopeCategory` 型を強制
  - `AssessmentResult` type alias の identity (`InScope | OutOfScope`)
- `tests/analysis/classifier/test_parse_assessment.py`:
  - flat dict が `OUT_OF_SCOPE` 値で `OutOfScope` を返す
  - flat dict が in-scope category で `InScope` を返す (`InScopeCategory` 詰め替え)
  - schema 違反 (key 欠落 / 型不一致 / enum 外値) で `AssessmentResponseInvalidError` raise
  - parse 結果が frozen
- `tests/analysis/classifier/test_envelope.py`:
  - `AssessmentCall` の field、frozen 性

### 不変条件

- AI への request schema (Gemini / DeepSeek の dict response_schema) は **変えない** ← 本 PR で classifier impl は touch しないので自然に守られる
- 既存テストで `AssessmentResponse` を import しているものを `AssessmentResult` に rename しても挙動同じ

### review 観点

- `ClassificationRawResponse` の残骸ゼロ (`grep -rn ClassificationRawResponse backend/`)
- `InScope.category` の型が `InScopeCategory`、`ValidCategory.OUT_OF_SCOPE` を渡そうとすると mypy / runtime で reject される
- `parse_assessment` が provider 非依存 (gemini / deepseek 両方から呼ばれる前提のため)
- caller 追従漏れ (frontend types は不変、Pydantic v2 schema 変更時の type generate は不要)

---

## PR3 — Classifier 実装改修 (`_translate_error` / `_call_once`)

**目的**: classifier 戻り値を `AssessmentCall` 化、SDK 例外を `AIProvider*Error` に翻訳する責務を確定。

### スコープ

含む:
- `app/analysis/classifier/base.py` 改修
  - `classify` / `_call_api` の戻り型を `AssessmentResult` → `AssessmentCall`
  - `_translate_error` の戻り型を `AnalysisDomainError` → `Exception` (bare re-raise guard 規約)
  - `_call_once` で `(AIProviderError, AssessmentDomainError)` を素通し再 raise、未知例外は `_translate_error` を経由し `if translated is exc: raise`
- `app/analysis/classifier/gemini.py` 改修
  - `_call_api` で SDK dict response → `parse_assessment` → `AssessmentCall` 構築
  - `finish_reason == SAFETY` / `RECITATION` で `AIProviderOutputBlockedError` raise
  - `_translate_error` を spec §Gemini SDK 翻訳テーブル に従って書き直し
- `app/analysis/classifier/deepseek.py` 改修
  - 同上、OpenAI 互換 SDK 例外を spec §DeepSeek SDK 翻訳テーブル に従って翻訳
- `app/analysis/assessment/service.py` 軽微改修
  - `classifier.classify(...)` の戻り値 unpack を `AssessmentCall` 形式に
  - **provider 例外の ACL は本 PR では入れない** (PR6 で行う、本 PR では素通し → 旧経路で処理)

含まない:
- Service.execute() の ACL boundary 化 (PR6)
- AssessmentPayload / AuditRepository (PR5)

### 作業内訳

1. `base.py`:
   - 抽象メソッドの戻り型を `AssessmentCall` に変更
   - `_translate_error` の docstring と戻り型を緩める
   - `_call_once` を spec §Classifier 実装 の skeleton 通りに書き直し
2. `gemini.py`:
   - `_call_api` を全面書き直し: dict schema 渡し → response.text を `json.loads` → `parse_assessment` → `AssessmentCall` 構築
   - `finish_reason` チェックを `_call_api` 内で raise
   - `_translate_error` を spec table 通りに振り分け (status / message inspect)
3. `deepseek.py`:
   - 同上、`openai` SDK 例外を翻訳
4. `service.py`:
   - `classifier.classify(...)` の戻り値 unpack 経路を `AssessmentCall` 形式に
   - `call.result` / `call.raw_response` / `call.raw_category` / `call.raw_topic` / `call.prompt_version` を取り出す形に修正
   - **本 PR では Service の例外処理は変えない** (`AIProviderError` は素通し、Task 層が旧 except で捕捉)

### テスト戦略

- `tests/analysis/classifier/test_base_call_once.py`:
  - bare re-raise guard (`_translate_error` が exc をそのまま返したら `from exc` 付きラップせず raise)
  - `AIProviderError` 配下と `AssessmentDomainError` 配下の素通し
- `tests/analysis/classifier/test_gemini_translate_error.py`:
  - spec の翻訳テーブル全行を test (mock SDK 例外を投げて期待 `AIProvider*Error` を確認)
  - `finish_reason == SAFETY` で `AIProviderOutputBlockedError`
- `tests/analysis/classifier/test_deepseek_translate_error.py`:
  - 同上 (OpenAI SDK 例外群)
- 既存の Service テストは `AssessmentCall` 受けに rewrite

### 不変条件

- 既存テストの assertion ロジック (Service 経由で `pipeline_events` に何が焼かれるか) は本 PR では変えない (旧経路維持)
- AI request schema (dict 形式の response_schema) は新規追加だが、AI が返す flat JSON 形式自体は不変

### review 観点

- bare re-raise guard が `if translated is exc: raise` で正しく書かれているか (`from exc` を付けていないか)
- finish_reason チェックが `_call_api` 内、`_translate_error` 経由でない
- spec の翻訳テーブルと実装が 1 対 1 対応 (抜けゼロ、過剰なマッピングなし)
- `AIProviderError` の `KIND` ClassVar が classifier 側で参照されていない (classifier は KIND を見ない、CODE / INLINE_RETRY も Service 層で取得)

---

## PR4 — Alembic migration (Stage rename + category 追加)

**目的**: DB schema 側の準備。`Stage.CLASSIFICATION` → `ASSESSMENT` rename と `non_retryable_keep_extraction` 追加を一括 migration。

### スコープ

含む:
- `alembic/versions/<rev>_assessment_audit.py` 新規 migration
  - `pipeline_events.stage` CHECK 制約に `'assessment'` 追加
  - 既存 row の `stage='classification'` → `'assessment'` 一括 UPDATE
  - 既存 row の `payload->>'kind'='classification'` → `'assessment'` 一括 UPDATE (JSONB)
  - 旧値 `'classification'` を CHECK 制約から削除
  - `pipeline_events.category` CHECK 制約に `'non_retryable_keep_extraction'` 追加
- `app/observability/categories.py` 改修
  - `Stage.CLASSIFICATION` → `Stage.ASSESSMENT` rename (enum 値変更)
  - `Layer1Category.NON_RETRYABLE_KEEP_EXTRACTION = "non_retryable_keep_extraction"` 追加

含まない:
- `AssessmentPayload` schema (PR5)
- 書き込み経路の追加 (PR5/PR6)

### 作業内訳

1. enum 改修:
   - `Stage.CLASSIFICATION = "classification"` を削除
   - `Stage.ASSESSMENT = "assessment"` を追加
   - `Layer1Category` に新値追加
2. Alembic migration:
   - `op.execute("ALTER TABLE pipeline_events DROP CONSTRAINT pipeline_events_stage_check")`
   - 新 CHECK 制約 (`'assessment'` を含む集合)
   - `UPDATE pipeline_events SET stage='assessment' WHERE stage='classification'`
   - `UPDATE pipeline_events SET payload = jsonb_set(payload, '{kind}', '"assessment"') WHERE payload->>'kind' = 'classification'`
   - 旧 CHECK 制約を再 ALTER で削除値に
   - `category` CHECK 制約も同様に新値追加
   - downgrade は逆操作 (`'classification'` 復活、`'non_retryable_keep_extraction'` 削除前に row 検査)
3. enum import 利用箇所の追従 — 既存 code で `Stage.CLASSIFICATION` を参照している箇所を grep、`Stage.ASSESSMENT` に rename

### テスト戦略

- `tests/migrations/test_assessment_audit_migration.py` (もしくは integration test)
  - migration 適用前に `stage='classification'` の row を仕込み、適用後に `'assessment'` になる
  - downgrade で逆向き
  - CHECK 制約違反テスト (新値 / 旧値の reject 検証)
- `tests/observability/test_categories.py`:
  - `Stage.ASSESSMENT.value == "assessment"`
  - `Layer1Category.NON_RETRYABLE_KEEP_EXTRACTION.value == "non_retryable_keep_extraction"`

### 不変条件

- 既存 `pipeline_events` row は migration によって stage / payload.kind が更新されるが、その他 column は不変
- 他 stage (`extraction` / `embedding`) の row には影響しない
- migration は idempotent (再実行可能 — 既に `'assessment'` の row は no-op)

### review 観点

- alembic revision id が 32 文字以下 (memory: `feedback_alembic_revision_id_length.md` 参照)
- CHECK 制約の列挙値が完全 (既存値 + 新値、旧値削除)
- payload の jsonb update が `WHERE payload->>'kind' = 'classification'` で絞られている (extraction / embedding は除外)
- downgrade で `'assessment'` の row が `'classification'` に戻り、CHECK 制約の旧値も復活
- production deploy 時の lock 影響 (UPDATE 規模は数万行オーダー想定、deploy window で許容)

---

## PR5 — `AssessmentPayload` + `AssessmentAuditRepository`

**目的**: Stage 4 監査の payload / repository を整備。書き込み経路にはまだ繋がない (PR6 で繋ぐ)。

### スコープ

含む:
- `app/observability/domain/payloads.py` 改修 — `AssessmentPayload` 追加 (spec §AssessmentPayload の field 一覧)
- `app/analysis/assessment/audit_repository.py` 新規 — `AssessmentAuditRepository` (`append_in_scope` / `append_out_of_scope` / `append_failure`)

含まない:
- Service / Task からの呼び出し (PR6)
- `record_assessment_failure` helper (PR6)

### 作業内訳

1. `payloads.py`:
   - `AssessmentPayload(BasePipelineEventPayload)` 追加
   - `kind: Literal["assessment"]`、spec 表通りの field
   - discriminated union (`PipelinePayload`) に追加
2. `audit_repository.py` 新規:
   - 3 method (spec §AssessmentAuditRepository の skeleton 通り)
   - `_category_of` / `_code_of` (instance 属性 `exc.code` / `AssessmentTerminalSkipError` 判定)
   - `PipelineEventRepository` を compose

### テスト戦略

- `tests/observability/domain/test_assessment_payload.py`:
  - schema validation (必須 field / 型)
  - `kind="assessment"` 固定
  - discriminated union での parse 検証
- `tests/analysis/assessment/test_audit_repository.py`:
  - `append_in_scope` で正しい payload / category / code が pipeline_events に書き込まれる
  - `append_out_of_scope` 同上
  - `append_failure` で `AssessmentTerminalSkipError` → `Layer1Category.NON_RETRYABLE_KEEP_EXTRACTION`、`AssessmentRetryableError` → `Layer1Category.RETRYABLE`、`Exception` → `Layer1Category.UNKNOWN` の 3 経路
  - `exc.code` instance 属性が `code` column に焼かれる
  - commit しないこと (caller が tx 境界を握る)

### 不変条件

- まだ caller がいないので、production 挙動への影響ゼロ
- 既存 `ExtractionPayload` / `ExtractionAuditRepository` には影響しない
- migration (PR4) で追加した `Stage.ASSESSMENT` / `non_retryable_keep_extraction` を使う

### review 観点

- payload の field が spec 表と完全一致 (raw_category / raw_topic / category_slug / topic / investor_take / prompt_version / ai_model / ai_raw_response / input_text / input_text_length / extraction_id)
- payload に **state representation を持たない** (top-level column と二重化禁止、article_id を持たない)
- `_category_of` で `AssessmentTerminalSkipError` を `NON_RETRYABLE_KEEP_EXTRACTION` にマップ (spec の意図的な命名差)
- `_code_of` が instance 属性 (`getattr(exc, "code", None)`) を見る (ClassVar `CODE` ではなく)

---

## PR6 — Service ACL + Task dispatch + `record_assessment_failure`

**目的**: 本番挙動の最終切り替え。Service 層 ACL boundary、Task 層 2-marker dispatch、audit 焼付の稼働。

### スコープ

含む:
- `app/analysis/assessment/service.py` 改修
  - `execute()` で `AIProviderError` を catch → `map_provider_to_assessment` で詰め替え re-raise
  - `_handle_in_scope` / `_handle_out_of_scope` で **同 tx audit append** を入れる (`AssessmentAuditRepository.append_in_scope` / `append_out_of_scope`)
  - race 敗北時は audit 焼かない (`saved is None` 経路)
- `app/analysis/assessment/failure_recording.py` 新規 — `record_assessment_failure(session_factory, ready, exc, attempt)`
- `app/analysis/tasks.py::assess_content` 改修
  - 旧 except 群を **3 except** (`AssessmentRetryableError` / `AssessmentTerminalSkipError` / `Exception`) に置き換え
  - `exc.inline_retry` instance 属性経由の inline retry 判定
  - 失敗経路から `record_assessment_failure` を呼ぶ
  - 旧 import (`ConfigurationError` / `DailyQuotaExhaustedError` / `InsufficientBalanceError` / `RateLimitError as AnalysisRateLimitError` / `ProviderError` / `NetworkError` / `UnclassifiedError`) を削除

含まない (本 spec scope 外):
- 既存 `record_extraction_failure` の改修 (Stage 3 互換維持)
- 他 stage の影響

### 作業内訳

1. `service.py`:
   - `execute()` 内で `try: classifier.classify(...) except AIProviderError as exc: raise map_provider_to_assessment(exc) from exc`
   - `_handle_in_scope` で `audit_repo.append_in_scope(...)` を業務 INSERT と同 session で commit
   - `_handle_out_of_scope` 同上
   - race 敗北時は `find_by_extraction_id` で winner 読戻し + audit skip
2. `failure_recording.py` 新規:
   - spec §record_assessment_failure の skeleton 通り
   - audit INSERT 失敗時は `logger.exception` + return (吞む)
3. `tasks.py::assess_content` 改修:
   - 全面書き直し (旧 except 構造を破棄)
   - 3 except + catch-all
   - 成功時 chain (`InScopeOutcome` → `generate_embedding.kiq`) は維持

### テスト戦略

- `tests/analysis/assessment/test_service_acl.py`:
  - Service.execute() が classifier raise の `AIProviderRateLimitedError` を `AssessmentProviderRetryableError` に詰め替え
  - 各 KIND が正しい marker に詰め替え (9 種)
  - Stage 4 specific 例外 (`AssessmentResponseInvalidError`) は素通し
- `tests/analysis/assessment/test_service_in_scope_audit.py`:
  - `_handle_in_scope` 成功時に `pipeline_events` row が同 tx で書き込まれる
  - race 敗北時は audit 焼かない
- `tests/analysis/assessment/test_service_out_of_scope_audit.py`:
  - 同上 (out-of-scope 経路)
- `tests/analysis/assessment/test_record_assessment_failure.py`:
  - Stage 4 marker 各種で正しい category / code / payload
  - audit INSERT 失敗を吞む
- `tests/test_analysis_tasks.py::assess_content` を改修:
  - 旧 except case を削除
  - 3 except dispatch の各経路 (Retryable inline / Retryable last attempt / TerminalSkip / catch-all)
  - `exc.inline_retry` 経由の inline retry 判定
  - 成功時 chain (Stage 5 へ)

### 不変条件

- Stage 5 (`generate_embedding`) との chain は不変 (`InScopeOutcome` のみ chain)
- `Pattern A'` (`ReadyForAssessment.try_advance_from`) で Service 到達前に idempotent skip する経路は変えない
- Stage 3 (`extract_content`) は touch しない

### review 観点

- `service.py` の except が `AIProviderError` のみ (Stage 4 specific を一緒に catch しない、素通し)
- task の except が **3 つだけ** (`AssessmentRetryableError` / `AssessmentTerminalSkipError` / `Exception`)、`isinstance` chain ゼロ
- `exc.inline_retry` を見る (`type(exc).INLINE_RETRY` を見ない)
- 旧 import (Layer 2-A の Analysis*Error 群) が完全削除されている (`grep -rn "from app.analysis.errors import" backend/app/analysis/tasks.py`)
- failure_recording で audit 失敗を吞む warning ログが残る
- 同 tx audit (in_scope / out_of_scope) と別 tx audit (failure) の使い分けが正しい

---

## 全体テスト戦略

### 各 PR で必須

- `uv run ruff check app/`
- `uv run ruff format --check app/`
- `uv run pytest tests/ -x -q`

### Integration (PR4 / PR5 / PR6 で重視)

- `make test-integration` で db-test 経由の統合テスト
  - PR4: migration 適用テスト
  - PR5: AuditRepository の DB 書き込み
  - PR6: Service 経由の同 tx 動作 (in-scope の row + audit row が同 commit で確定)

### 既存テスト互換

- PR2 で `AssessmentResponse` → `AssessmentResult` の rename によるテスト更新
- PR3 で classifier 戻り値が `AssessmentCall` に変わることによる既存テスト更新
- PR6 で `assess_content` task の 3 except 化による旧 case 削除

---

## deploy 順序の注意

| PR | deploy タイミング | 注意 |
|---|---|---|
| PR1 | 即時 deploy 可 | 新規 import なし、影響なし |
| PR2 | 即時 deploy 可 | rename + 内部詰め替え、挙動同じ |
| PR3 | 即時 deploy 可 | classifier 戻り型変更、Service 軽微追従、本番挙動同じ |
| PR4 | **deploy window 推奨** | 数万行 UPDATE、production lock 影響を許容範囲で |
| PR5 | 即時 deploy 可 | 新規ファイルのみ、書き込み経路に繋がっていない |
| PR6 | **PR4 deploy 確認後 deploy** | DB schema (PR4) と書き込み経路 (PR6) が整合する状態で deploy |

順序: PR1 → PR2 → PR3 → PR4 → PR5 → PR6 が最も安全。PR1〜3 と PR4〜5 は並列着手可だが、merge 順序は依存に従う。

---

## 関連仕様

- `specs/pipeline-events-stage4-assessment.md` — 本 plan が実装対象とする確定 spec
- `specs/pipeline-events-error-taxonomy.md` — foundation Layer 1 marker / DB schema
- `specs/pipeline-events-stage3-extraction.md` — Stage 3 確定仕様 (Stage 4 はこの構造を Stage 4 文脈で起こし直す)
