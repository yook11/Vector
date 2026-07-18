# Logfire Curation Processing Outcome Metrics

作成: 2026-06-15
Status: Implemented (PR #814)

---

## Work Definition

### Problem

curation stage について、Logfire 上で「インフラ障害に汚されない処理成功率」を可視化する。

ここで見たいのは AI 分類だけの成功率ではなく、ready-build から curation 実行までを含めた curation 処理としての成功率である。ただし DB / Redis / queue などのインフラ障害は処理品質とは別なので、成功率の分母からは外す。

### Evidence

- `curate_content` task は `ReadyForCuration.try_advance_from()` で curation 入力を構築してから、rate limit gate と `CurationService.execute()` に進む。
- `CurationReadyBuildBlockedCode` は `ARTICLE_MISSING`, `ALREADY_CURATED`, `ALREADY_REJECTED_AS_NOISE`, `CONTENT_TOO_LARGE` を区別する。
- `CurationFailureHandler.handle()` は `CurationTerminalDropError`, `CurationTerminalKeepError`, `CurationRecoverableError`, `SQLAlchemyError`, catch-all を分岐している。
- `article_stage` span result は `signal`, `noise`, `rate_limited`, `skipped`, `failed` だが、`skipped` と `failed` は処理結果・冪等 skip・インフラ失敗を区別できない。
- taskiq の OTel middleware が `execute/curate_content` span を自動で作るため、task が例外で落ちたかどうかは既存 span から観測できる。
- Redis / queue / rate limit gate 由来の例外は、現状 `CurationFailureHandler.handle()` の分類境界に入らないか catch-all に落ちるため、初期 `infra_error` としては分類できない。
- `curate_content` は retry されうるため、この metric は記事単位ではなく処理試行単位になる。

### Invariants

- 本 metric は処理試行単位の指標であり、記事単位の最終成功率ではない。
- `signal` と `noise` は curation 処理成功として扱う。
- `rejected` と `failed` は curation 処理成功率の分母に含める。
- `infra_error` は emit するが、curation 処理成功率の分母には含めない。
- `rate_limited`, 冪等 skip, race loss, `ARTICLE_MISSING` は成功率の分母から除外する。
- `infra_error` は全インフラ失敗の総数ではなく、この metric の分類境界で infra と断定できる handled/classified failure だけを表す。
- metric attribute に `article_id`, `source_id`, source 名, URL, prompt, raw response, error message, model, prompt version は載せない。
- `vector.curation.processing_outcome` は span-shadow ではない。分類が判明する task / service / handler 境界で emit する。

### Non-goals

- provider / model / prompt version 別 breakdown は扱わない。
- source 別 curation 成功率は扱わない。
- `failure_kind` label は追加しない。
- `rate_limited` 率、skipped 率は初期ダッシュボードで扱わない。
- `stage_attempt` counter は追加しない。
- `pipeline_events` schema は変更しない。
- assessment / embedding / completion への横展開は今回の対象外。
- 記事単位の重複排除済み成功率は扱わない。

### Done

- Logfire metric `vector.curation.processing_outcome{result}` の仕様が定義される。
- `result` attribute は `signal`, `noise`, `rejected`, `failed`, `infra_error` の 5 値だけを持つ。
- dashboard 指標と分母の扱いが明文化される。
- `stage_attempt` を今回追加しない理由が明文化される。
- 実装時に必要な emit point と分類境界が明文化される。

---

## 1. Metric Contract

### 1.1 Metric

```text
metric: vector.curation.processing_outcome
type: counter
unit: 1
attributes:
  result = signal | noise | rejected | failed | infra_error
```

`processing_outcome` は、curation stage の処理結果を表す。

これは `article_stage` span result の完全ミラーではない。span の `skipped` / `failed` は粗く、冪等 skip・処理失敗・インフラ失敗を区別できないため、metric は分類が判明する境界で emit する。

### 1.2 Result Vocabulary

#### signal

curation が関連ありと判断した。処理成功。assessment へ進む。

#### noise

curation が関連なしと判断した。処理成功。ここで止める。

#### rejected

curation 処理内で、対象として受け付けないと明示判断した。

初期対象:

```text
CONTENT_TOO_LARGE
```

`CONTENT_TOO_LARGE` は対象記事が存在し、内容を見たうえで curation 入力として拒否しているため、成功率の分母に含める。

#### failed

curation 処理に入ったが、有効な `signal` / `noise` に到達できなかった。

初期対象:

```text
CurationTerminalDropError
CurationTerminalKeepError
CurationRecoverableError
catch-all unexpected
```

`CurationTerminalDropError` は AI 処理に入ったうえで有効な `signal` / `noise` に到達できなかったため、`failed` に倒す。

infra と断定できない想定外例外も、原則 `failed` に倒す。ここにはエラーハンドリング漏れやコードバグが混ざる可能性があるため、安易に成功率の分母から除外しない。

#### infra_error

DB / Redis / queue など、curation 処理ロジック外の失敗。

初期対象:

```text
SQLAlchemyError
DB error として分類された ready-build failed
```

`infra_error` は成功率の分母から外す。ただし count としては可視化し、dashboard から消さない。

Redis / queue / rate limit gate 由来の例外は、初期実装では `infra_error` に含めない。現状は handler の分類境界に入らないか catch-all に落ちるため、task が落ちる場合は `execute/curate_content` span の ERROR で見る。将来 `infra_error` に寄せる場合は、handler または task 境界で明示分類を追加する。

---

## 2. Excluded Outcomes

以下は `vector.curation.processing_outcome` に emit しない。

```text
rate_limited
ALREADY_CURATED
ALREADY_REJECTED_AS_NOISE
ARTICLE_MISSING
race loss
```

### 2.1 rate_limited

rate limit による停止は処理品質ではなく capacity 制御である。既存の `vector.analysis.rate_limit_gate_skipped{stage=curation}` でも観測できるため、初期 metric では emit しない。

### 2.2 ALREADY_* / Race Loss

`ALREADY_CURATED`, `ALREADY_REJECTED_AS_NOISE`, race loss は冪等性・重複実行の揺れであり、処理成功率の分母に混ぜない。

### 2.3 ARTICLE_MISSING

`ARTICLE_MISSING` は対象自体が存在しない stale / race 系として扱い、初期 metric では emit しない。

`CONTENT_TOO_LARGE` のように対象の内容を見たうえで拒否するケースとは異なる。

---

## 3. Dashboard Metrics

指定 window 内の `vector.curation.processing_outcome` を `result` 別に集計する。

### 3.1 Counts

```text
signal_count      = count(result = signal)
noise_count       = count(result = noise)
rejected_count    = count(result = rejected)
failed_count      = count(result = failed)
infra_error_count = count(result = infra_error)
```

### 3.2 Curation Success Percent

```text
curation_success_percent =
  100 * (signal_count + noise_count)
  / NULLIF(signal_count + noise_count + rejected_count + failed_count, 0)
```

意味:

curation の処理試行が、インフラ障害を除いて有効な `signal` / `noise` に到達した割合。

`infra_error` は分母に入れない。インフラ障害は処理品質ではないため成功率を汚さないが、`infra_error_count` として別に見る。

`infra_error_count` は全インフラ失敗の総数ではない。Redis / queue / gate 例外のように task ごと落ちる失敗は、初期実装では `execute/curate_content` span の ERROR 側で見る。

### 3.3 Signal Share Percent

```text
signal_share_percent =
  100 * signal_count
  / NULLIF(signal_count + noise_count, 0)
```

意味:

成功した curation 処理のうち、assessment へ進む signal の割合。

`noise` の割合は `100 - signal_share_percent` として読めるため、初期指標として独立した `noise_share_percent` は持たない。

### 3.4 Initial Dashboard

初期ダッシュボードでは以下だけを表示する。

```text
curation_success_percent
signal_share_percent
signal_count
noise_count
rejected_count
failed_count
infra_error_count
```

---

## 4. Emit Policy

### 4.1 Not Span-shadow

`vector.curation.processing_outcome` は `CurationStageSpan.set_result()` から一律 emit しない。

理由:

- span result `skipped` は ready-build blocked, race loss, rate-limited などを区別できない。
- span result `failed` は AI / parse / provider 失敗、DB エラー、backstop 失敗を区別できない。
- 今回の指標は span の見た目ではなく、処理成功率の意味論に合わせる必要がある。

### 4.2 Emit Points

#### signal / noise

`CurationService.execute()` が `signal` / `noise` を保存し、成功 audit と同一 transaction を commit した後に emit する。

race loss で `None` を返す場合は emit しない。

#### rejected

`CurationReadyBuildBlockedError` のうち、処理対象として明示拒否したものだけ emit する。

初期対象:

```text
CONTENT_TOO_LARGE -> rejected
```

#### failed / infra_error

`CurationFailureHandler.handle()` が失敗分類を確定した後に emit する。

handler は decision に `processing_outcome` 用の分類を返す必要がある。

初期分類:

```text
CurationTerminalDropError    -> failed
CurationTerminalKeepError    -> failed
CurationRecoverableError     -> failed
SQLAlchemyError              -> infra_error
catch-all unexpected         -> failed
```

ready-build 中の blocked 以外の例外は、既存の ready-build failure projection に基づき分類する。

```text
ready_build_failed_db_error          -> infra_error
ready_build_failed_contract_invalid  -> failed
ready_build_failed_unexpected_error  -> failed
```

ready-build failed は、そのまま例外を再送出すると context manager backstop に到達する。backstop だけでは `processing_outcome` を emit しないため、`curate_content` の ready-build `except Exception` 節内で projection 分類し、再送出前に emit する必要がある。audit は best-effort であり、audit drop は metric emit を抑止しない。

`CurationFailureHandler.handle()` が返す classification は、共有 `FailureHandlingDecision` へ無条件に拡張する前に他 stage への波及を確認する。波及が大きい場合は、curation 専用 decision 型または別戻り値で `processing_outcome` を返す。

`CurationRecoverableError` は retry 前提の transient failure を含みうるが、本 metric は処理試行単位のため、retry 前の recoverable failure も `failed` として数える。記事単位の最終成功率は本 metric の対象外である。

---

## 5. Stage Attempt Counter

今回は `vector.curation.stage_attempt` counter は追加しない。

理由:

- task が例外で落ちたかどうかは `execute/curate_content` span の ERROR で既に見られる。
- 今回の主目的は stage 実行の信頼性ではなく、curation 処理成功率である。
- handled な DB エラーは `processing_outcome{result=infra_error}` として可視化するため、dashboard から消えない。
- metrics テーブル統一や sampling 耐性が必要になった時点で、別途 `stage_attempt` を検討すればよい。

---

## 6. Test Requirements

### 6.1 Metric Emit

- signal 保存 + audit commit 後に `processing_outcome{result=signal}` が +1 される。
- noise 保存 + audit commit 後に `processing_outcome{result=noise}` が +1 される。
- `CONTENT_TOO_LARGE` の ready-build blocked で `processing_outcome{result=rejected}` が +1 される。
- `CurationTerminalDropError`, `CurationTerminalKeepError`, `CurationRecoverableError` で `processing_outcome{result=failed}` が +1 される。
- `SQLAlchemyError` で `processing_outcome{result=infra_error}` が +1 される。

### 6.2 Non-emitted Cases

- `rate_limited` は emit されない。
- `ALREADY_CURATED` は emit されない。
- `ALREADY_REJECTED_AS_NOISE` は emit されない。
- `ARTICLE_MISSING` は emit されない。
- race loss は emit されない。

### 6.3 Attribute Safety

- data point attributes は `{"result": <value>}` のみ。
- metric dump に `article_id`, `source_id`, URL, prompt, raw response, error message, model, prompt version が混入しない。

### 6.4 Backstop

- context manager backstop の `failed` だけでは `processing_outcome` を emit しない。
- backstop は span 可視性の安全網であり、processing outcome の分類根拠ではない。
