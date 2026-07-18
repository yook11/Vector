# Logfire Assessment Processing Outcome Metrics

作成: 2026-06-16
Status: Implemented (PR #816)

関連: [`logfire-curation-outcome-metrics.md`](./logfire-curation-outcome-metrics.md) (curation 同型先行例、PR #814)

---

## Work Definition

### Problem

assessment stage (Stage 4) について、Logfire 上で「インフラ障害に汚されない処理成功率」を可視化する。

見たいのは AI 判定だけの成功率ではなく、ready-build から assessment 実行までを含めた assessment 処理としての成功率である。ただし DB / Redis / queue などのインフラ障害は処理品質とは別なので、成功率の分母からは外す。curation で確立した `processing_outcome` パターンを assessment へ展開する。

### Evidence

- `assess_content` task は `ReadyForAssessment.try_advance_from()` で assessment 入力を構築してから、rate limit gate と `AssessmentService.execute()` に進む。
- `AssessmentService.execute()` は AI 判定を `in_scope` / `out_of_scope` に振り分け、業務行と成功 audit を同一 transaction で commit してから、span result を `in_scope` / `out_of_scope` に焼く。楽観ロック敗北 (save が `None`) では audit / commit せず span result を `skipped` にして `None` を返す。
- assessment は原文を読まず、curation 済みの翻訳 title / summary だけを判定する。したがって curation の `CONTENT_TOO_LARGE` のような「内容を見たうえで拒否する」経路は存在しない。
- `AssessmentReadyBuildBlockedCode` は `CURATION_MISSING`, `ALREADY_IN_SCOPE`, `ALREADY_OUT_OF_SCOPE` の 3 つで、すべて precondition (上流消失・既処理) 由来の stale / 冪等系である。
- `AssessmentFailureHandler.handle()` は `AssessmentTerminalError`, `AssessmentRecoverableError`, `SQLAlchemyError`, catch-all を分岐する。curation と異なり内容起因 DELETE (Drop) 経路を持たない (curation を保持して assessment を skip する設計)。各 marker は `_audit_failure` / `_audit_unexpected_failure` (どちらも自前で例外を握る best-effort) を経由する。
- `AssessmentFailureHandler.handle()` は `assessor` を引数に取らない (`ready`, `exc`, `last_attempt` のみ)。
- ready-build 中の blocked 以外の例外は、共有 `project_ready_build_failure(stage_prefix="assessment", exc=exc)` で `db_error` / `contract_invalid` / `unexpected_error` に分類できる。
- `article_stage` span result は `in_scope`, `out_of_scope`, `rate_limited`, `skipped`, `failed` だが、`skipped` と `failed` は処理結果・冪等 skip・インフラ失敗を区別できない。
- taskiq の OTel middleware が `execute/assess_content` span を自動で作るため、task が例外で落ちたかどうかは既存 span から観測できる。
- `assess_content` は `max_retries=2` で retry され、`SQLAlchemyError`・catch-all も `reraise=not last_attempt` で retry されうる。

### Invariants

- 本 metric は処理試行単位で集計する (記事単位の最終成功率ではない)。
- `in_scope` と `out_of_scope` は assessment 処理成功として扱う (AI が有効な判定に到達した)。
- `failed` は assessment 処理成功率の分母に含める。
- `infra_error` は emit するが、assessment 処理成功率の分母には含めない。
- `rate_limited`, 冪等 skip, race loss, ready-build blocked 全コードは成功率の分母から除外する。
- `infra_error` は全インフラ失敗の総数ではなく、この metric の分類境界で infra と断定できる handled/classified failure だけを表す。
- metric attribute に `curation_id`, `analyzable_article_id`, `source_id`, source 名, URL, prompt, raw response, error message, model, prompt version は載せない。
- `vector.assessment.processing_outcome` は span-shadow ではない。分類が判明する task / service / handler 境界で emit する。
- 失敗分類は handler の match 時点で確定するため、audit などの副作用より先に emit する (curation #814 の学び。assessment は Drop が無いため取りこぼしリスクは低いが原則を揃える)。

### Non-goals

- `rejected` 値は持たない。assessment には content-based reject 経路が無いため、将来の対称性だけを理由に追加しない。
- provider / model / prompt version 別 breakdown は扱わない。
- source 別 assessment 成功率は扱わない。
- `failure_kind` label は追加しない。
- `rate_limited` 率、skipped 率は初期ダッシュボードで扱わない。
- `stage_attempt` counter は追加しない。
- `pipeline_events` schema は変更しない。
- 共有 `FailureHandlingDecision` (curation / assessment / embedding 共有) は拡張しない。分類は handler が直接 emit する。
- embedding / completion への横展開は今回の対象外。

### Done

- Logfire metric `vector.assessment.processing_outcome{result}` の仕様が定義される。
- `result` attribute は `in_scope`, `out_of_scope`, `failed`, `infra_error` の 4 値だけを持つ。
- dashboard 指標と分母の扱いが明文化される。
- `rejected` を持たない理由が明文化される。
- `stage_attempt` を今回追加しない理由が明文化される。
- 実装時に必要な emit point と分類境界が明文化される。

---

## 1. Metric Contract

### 1.1 Metric

```text
metric: vector.assessment.processing_outcome
type: counter
unit: 1
attributes:
  result = in_scope | out_of_scope | failed | infra_error
```

`processing_outcome` は、assessment stage の処理結果を表す。

これは `article_stage` span result の完全ミラーではない。span の `skipped` / `failed` は粗く、冪等 skip・処理失敗・インフラ失敗を区別できないため、metric は分類が判明する境界で emit する。

### 1.2 Result Vocabulary

#### in_scope

assessment が投資関連ありと判定し、永続化した。処理成功。embedding へ進む。

#### out_of_scope

assessment が投資関連なしと判定し、永続化した。処理成功。ここで止める。

`out_of_scope` は curation の `noise` に相当する。AI が有効な判定に到達した成功であり、`rejected` ではない。

#### failed

assessment 処理に入ったが、有効な `in_scope` / `out_of_scope` に到達できなかった。

初期対象:

```text
AssessmentTerminalError
AssessmentRecoverableError
catch-all unexpected
ready_build_failed_contract_invalid
ready_build_failed_unexpected_error
```

infra と断定できない想定外例外も、原則 `failed` に倒す。ここにはエラーハンドリング漏れやコードバグが混ざる可能性があるため、安易に成功率の分母から除外しない。

#### infra_error

DB など、assessment 処理ロジック外の失敗。

初期対象:

```text
SQLAlchemyError
DB error として分類された ready-build failed (ready_build_failed_db_error)
```

`infra_error` は成功率の分母から外す。ただし count としては可視化し、dashboard から消さない。

Redis / queue / rate limit gate 由来の例外と timeout (CancelledError) は、初期実装では `infra_error` に含めない。これらは handler の分類境界に入らず、task が落ちる場合は `execute/assess_content` span の ERROR で観測する。

---

## 2. Excluded Outcomes

以下は `vector.assessment.processing_outcome` に emit しない。

```text
rate_limited
CURATION_MISSING
ALREADY_IN_SCOPE
ALREADY_OUT_OF_SCOPE
race loss
```

### 2.1 rate_limited

rate limit による停止は処理品質ではなく capacity 制御である。既存の `vector.analysis.rate_limit_gate_skipped{stage=assessment}` でも観測できるため、初期 metric では emit しない。

### 2.2 Ready-build Blocked (全コード)

`CURATION_MISSING` は上流 curation が存在しない stale、`ALREADY_IN_SCOPE` / `ALREADY_OUT_OF_SCOPE` は冪等・重複実行の揺れである。いずれも処理成功率の分母に混ぜない。curation と異なり、assessment の ready-build blocked には content-based reject (= `rejected`) が無いため、`rejected` 値自体を持たない。

### 2.3 Race Loss

楽観ロック敗北 (save_in_scope / save_out_of_scope が `None`) は重複実行の揺れであり、commit に到達しないため処理成功でも失敗でもない。分母に混ぜない。

---

## 3. Dashboard Metrics

指定 window 内の `vector.assessment.processing_outcome` を `result` 別に集計する。

### 3.1 Counts

```text
in_scope_count     = count(result = in_scope)
out_of_scope_count = count(result = out_of_scope)
failed_count       = count(result = failed)
infra_error_count  = count(result = infra_error)
```

### 3.2 Assessment Success Percent

```text
assessment_success_percent =
  100 * (in_scope_count + out_of_scope_count)
  / NULLIF(in_scope_count + out_of_scope_count + failed_count, 0)
```

意味:

assessment の処理試行が、インフラ障害を除いて有効な `in_scope` / `out_of_scope` に到達した割合。

`infra_error` は分母に入れない。インフラ障害は処理品質ではないため成功率を汚さないが、`infra_error_count` として別に見る。

`infra_error_count` は全インフラ失敗の総数ではない。Redis / queue / gate 例外や timeout のように task ごと落ちる失敗は、初期実装では `execute/assess_content` span の ERROR 側で見る。

### 3.3 In-Scope Share Percent

```text
in_scope_share_percent =
  100 * in_scope_count
  / NULLIF(in_scope_count + out_of_scope_count, 0)
```

意味:

成功した assessment 処理のうち、投資関連ありと判定され embedding へ進む割合。funnel 指標であり信頼性指標ではない。

### 3.4 Initial Dashboard

初期ダッシュボードでは以下だけを表示する。

```text
assessment_success_percent
in_scope_share_percent
in_scope_count
out_of_scope_count
failed_count
infra_error_count
```

---

## 4. Emit Policy

### 4.1 Not Span-shadow

`vector.assessment.processing_outcome` は `AssessmentStageSpan.set_result()` から一律 emit しない。

理由:

- span result `skipped` は ready-build blocked, race loss, rate-limited などを区別できない。
- span result `failed` は AI / parse / provider 失敗、DB エラー、backstop 失敗を区別できない。
- 今回の指標は span の見た目ではなく、処理成功率の意味論に合わせる必要がある。

### 4.2 Emit Points

curation (4 境界) と異なり、assessment は `rejected` が無いため **3 境界**で emit する。

#### in_scope / out_of_scope

`AssessmentService.execute()` が結果を保存し、成功 audit と同一 transaction を commit した後に emit する (既存の `set_assessment_stage_result` 呼び出しの隣)。

race loss で `None` を返す場合は emit しない。

#### failed / infra_error (handler)

`AssessmentFailureHandler.handle()` の各 match arm が、副作用 (`_audit_failure` / `_audit_unexpected_failure`) より先に emit する。

```text
AssessmentTerminalError      -> failed
AssessmentRecoverableError   -> failed
SQLAlchemyError              -> infra_error
catch-all unexpected         -> failed
```

handler は分類を共有 `FailureHandlingDecision` に載せず、自身で emit する (durability 境界の所有者が出す)。handler は `assessor` を取らないため、curation よりさらに単純。

#### failed / infra_error (ready-build failed)

ready-build 中の blocked 以外の例外は、`project_ready_build_failure(stage_prefix="assessment", exc=exc)` に基づき分類する。

```text
ready_build_failed_db_error          -> infra_error
ready_build_failed_contract_invalid  -> failed
ready_build_failed_unexpected_error  -> failed
```

`assess_content` の ready-build `except Exception` 節内で projection 分類し、再送出前に emit する。audit (`_append_ready_build_failed_audit`) は best-effort であり、audit drop は metric emit を抑止しない。再送出は span backstop に到達し span result を `failed` にするが、backstop は `processing_outcome` を emit しないため二重計上しない。

### 4.3 Backstop Does Not Emit

context manager backstop の `failed` だけでは `processing_outcome` を emit しない。backstop は span 可視性の安全網であり、processing outcome の分類根拠ではない。

これにより、分類境界をすり抜けて backstop だけに到達する失敗 (gate の Redis 例外、180s timeout の CancelledError、その他 BaseException) は `processing_outcome` に計上されない。これらは `execute/assess_content` span の ERROR ステータスで観測する設計境界とする (curation と同じ方針)。

---

## 5. Stage Attempt Counter

今回は `vector.assessment.stage_attempt` counter は追加しない。

理由:

- task が例外で落ちたかどうかは `execute/assess_content` span の ERROR で既に見られる。
- 今回の主目的は stage 実行の信頼性ではなく、assessment 処理成功率である。
- handled な DB エラーは `processing_outcome{result=infra_error}` として可視化するため、dashboard から消えない。
- metrics テーブル統一や sampling 耐性が必要になった時点で、別途 `stage_attempt` を検討すればよい。

---

## 6. Test Requirements

curation (`tests/analysis/curation/`) と同じハーネス分担に倣う。helper は `tests/logfire/_metric_helpers.py` を再利用する。

### 6.1 Metric Emit

- in_scope 保存 + audit commit 後に `processing_outcome{result=in_scope}` が +1 される。
- out_of_scope 保存 + audit commit 後に `processing_outcome{result=out_of_scope}` が +1 される。
- `AssessmentTerminalError`, `AssessmentRecoverableError`, catch-all で `processing_outcome{result=failed}` が +1 される。
- `SQLAlchemyError` で `processing_outcome{result=infra_error}` が +1 される。
- ready-build SQLAlchemyError で `infra_error`、ValidationError / その他で `failed` が +1 される。
- 各 arm は割り当て外の result を一切 emit しない (vocabulary 排他、4 値全て検証)。

### 6.2 Non-emitted Cases

- `rate_limited` は emit されない。
- `CURATION_MISSING` は emit されない。
- `ALREADY_IN_SCOPE` は emit されない。
- `ALREADY_OUT_OF_SCOPE` は emit されない。
- race loss (save が None) は emit されない。

### 6.3 Attribute Safety

- data point attributes は `{"result": <value>}` のみ。
- metric dump に `curation_id`, `analyzable_article_id`, `source_id`, URL, prompt, raw response, error message, model, prompt version が混入しない。

### 6.4 Backstop

- context manager backstop の `failed` だけでは `processing_outcome` を emit しない。
- backstop は span 可視性の安全網であり、processing outcome の分類根拠ではない。

### 6.5 Emit Independence From Audit

§4.2 の「分類は match 時点で確定し副作用より先に emit する / audit drop は emit を抑止しない」を固定する。

- handler の `_audit_failure` / `_audit_unexpected_failure` が DB 失敗 (audit drop) しても、`failed` / `infra_error` は emit される。
- ready-build failed の audit (`_append_ready_build_failed_audit`) が drop されても、`failed` / `infra_error` は emit される。
