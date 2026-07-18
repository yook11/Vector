# Logfire Embedding Processing Outcome Metrics

作成: 2026-06-17
Status: Implemented (PR #817)

関連:
- [`logfire-curation-outcome-metrics.md`](./logfire-curation-outcome-metrics.md) (curation 先行例、PR #814)
- [`logfire-assessment-outcome-metrics.md`](./logfire-assessment-outcome-metrics.md) (assessment 同型先行例、PR #816)

---

## Work Definition

### Problem

embedding stage (Stage 5) について、Logfire 上で「インフラ障害に汚されない処理成功率」を可視化する。

見たいのは「ready-build から embedding 保存までを含めた処理としての成功率」である。ただし DB / AI provider / 環境・設定・課金などインフラ・運用側の失敗は処理品質とは別なので、成功率の分母からは外す。curation / assessment で確立した `processing_outcome` パターンを embedding へ展開する。

ただし embedding は curation / assessment と 2 点で異なり、metric 設計を変える。

1. **判定スプリットが無い**。embedding は記事を分類せず、単にベクトルを生成して保存するだけなので、`in_scope` / `out_of_scope` のような成功の内訳が無い。成功は単一の `succeeded` だけになり、funnel 指標 (`in_scope_share` 相当) を持たない。
2. **失敗分類を domain の区切りで決めない**。AI provider 由来の失敗 (network / 5xx / rate limit / quota / config / balance / content 拒否) を「インフラ障害として分母外にするか / 処理失敗として分母に入れるか」は、domain の retry 軸 (`AIProviderFailureMode`) や class 系統 (`AIProviderStateError` / `AIProviderContentError`) と一致しない。metric 専用の分類軸を別に定義する (本 spec の中心。§2)。

### Evidence

- `generate_embedding` task は `ReadyForEmbedding.try_advance_from()` で入力を構築してから、rate limit gate と `EmbeddingService.execute()` に進む。Stage 5 はパイプライン終端で chain firing が無い。
- `EmbeddingService.execute()` は `embedder.embed_document()` の結果を条件付き保存し、保存成功時のみ成功 audit を同一 transaction で commit してから span result を `succeeded` に焼く。楽観ロック敗北 (save が `False`) では audit / commit せず span result を `skipped` にして返す。
- embedding には内容起因 DELETE (Drop) 経路が無い (analysis を保持して embedding を skip する設計)。
- `EmbeddingReadyBuildBlockedCode` は `ANALYZED_ARTICLE_MISSING`, `ALREADY_EMBEDDED` の 2 つで、いずれも precondition (上流消失・既処理) 由来の stale / 冪等系である。
- `EmbeddingFailureHandler.handle()` は `EmbeddingTerminalError`, `EmbeddingRecoverableError`, `SQLAlchemyError`, catch-all を分岐する。各 marker は `_audit_failure` / `_audit_unexpected_failure` (どちらも自前で例外を握る best-effort) を経由する。handler は `embedder` を引数に取らない (`ready`, `exc`, `last_attempt` のみ)。
- `EmbeddingService.execute()` の boundary で `to_embedding_error()` が `AIProviderError` を Stage 5 marker に詰め替える。retry 軸 (Recoverable / Terminal) は provider の `FAILURE_MODE.retryable` が一意に決める。marker は元の provider error を `provider_error` 属性に保持する。
- AI provider error は stage 中立な `app/analysis/ai_provider_errors.py` に定義され、2 系統に分かれる。`AIProviderStateError` (network / 5xx / quota / config 等、provider・環境の状態) と `AIProviderContentError` (safety / recitation / length 等、入出力の内容)。
- `AIProviderFailureMode` の `OPERATOR_ACTION_REQUIRED` は `AIProviderConfigurationError` / `AIProviderInsufficientBalanceError` / `AIProviderRequestInvalidError` の 3 つを束ねるが、これは retry / hold のための括りであり、「環境・課金で直る (infra)」と「こちらのコードが壊れた (stage 失敗)」を区別しない。
- `EmbeddingResponseInvalidError` は `EmbeddingRecoverableError` の派生で、`provider_error=None` を持つ (provider 応答が embedding schema に合致しない)。
- ready-build 中の blocked 以外の例外は、共有 `project_ready_build_failure(stage_prefix="embedding", exc=exc)` で `db_error` / `contract_invalid` / `unexpected_error` に分類できる。
- `article_stage` span result は `succeeded`, `rate_limited`, `skipped`, `failed` だが、`skipped` と `failed` は冪等 skip・race loss・処理失敗・インフラ失敗を区別できない。
- taskiq の OTel middleware が `execute/generate_embedding` span を自動で作るため、task が例外で落ちたかどうかは既存 span から観測できる。
- `generate_embedding` は `max_retries=2` で retry され、`EmbeddingRecoverableError`・`SQLAlchemyError`・catch-all も `reraise=not last_attempt` で retry されうる。

### Invariants

- 本 metric は処理試行単位で集計する (記事単位の最終成功率ではない)。
- `succeeded` は embedding 処理成功として扱う (ベクトルを生成し永続化した)。
- `failed` は embedding 処理成功率の分母に含める。
- `infra_error` は emit するが、embedding 処理成功率の分母には含めない。
- `rate_limited`, 冪等 skip, race loss, ready-build blocked 全コードは成功率の分母から除外する。
- **失敗の `infra_error` / `failed` 分類は、domain の retry 軸 (`AIProviderFailureMode`) や class 系統 (`AIProviderStateError` / `AIProviderContentError`) を流用しない。metric 専用の health-attribution 軸 (環境・設定・課金・依存先で直るか / stage 自身のコード・対象内容が原因か) で分類する (§2)。**
- 分類の SSoT は consumer (metric / handler) 側の明示分類とする。ドメインエラー class に集計 bucket を属性・メソッドとして持たせない。
- provider error の分類規則は stage に依らない (同じ network 障害はどの stage でも infra)。本 spec をその規則の SSoT とし、述語も stage 中立に書く。ただし本 PR で実際に consume するのは embedding のみで、assessment / curation への適用は別 PR とする (Non-goals 参照)。
- 全 `AIProviderStateError` leaf は `infra_error` / `failed` のどちらかに明示分類される (網羅テストで固定)。未分類の新エラーは silent に `infra_error` へ倒さず `failed` に倒す。
- `infra_error` は全インフラ失敗の総数ではなく、この metric の分類境界で infra と断定できる handled/classified failure だけを表す。
- metric attribute に `analyzed_article_id`, `analyzable_article_id`, `source_id`, source 名, URL, prompt, raw response, error message, model, vector dimension, failure_kind は載せない。
- `vector.embedding.processing_outcome` は span-shadow ではない。分類が判明する task / service / handler 境界で emit する。
- 失敗分類は handler の match 時点で確定するため、audit などの副作用より先に emit する (curation #814 の学び)。

### Non-goals

- 成功の内訳 (`in_scope` / `out_of_scope` のような分類スプリット) は持たない。embedding は記事を分類しないため、成功は単一の `succeeded`。
- funnel 指標 (`in_scope_share` 相当) は持たない。
- provider / model 別 breakdown は扱わない。
- source 別 embedding 成功率は扱わない。
- `failure_kind` label は追加しない (分類は `result` の 3 値に畳む)。
- `rate_limited` 率、skipped 率は初期ダッシュボードで扱わない。
- `stage_attempt` counter は追加しない。
- `pipeline_events` schema は変更しない。
- 共有 `FailureHandlingDecision` (curation / assessment / embedding 共有) は拡張しない。分類は handler が直接 emit する。
- completion (Stage 2) への横展開は今回の対象外。
- 本 PR は §2 の分類規則を **embedding にのみ**適用する。assessment (#816) / curation (#814) の metric 意味論を現行の DB-only から本分類へ変更するのは別 PR とし、それまで両 stage は DB-only を維持する。本 PR では両 stage の handler を書き換えない。

### Done

- Logfire metric `vector.embedding.processing_outcome{result}` の仕様が定義される。
- `result` attribute は `succeeded`, `failed`, `infra_error` の 3 値だけを持つ。
- 失敗分類規則 (`infra_error` vs `failed`) と、その背後の原則が明文化される。
- 分類が domain の retry 軸 / class 系統を流用しない理由が、`OPERATOR_ACTION_REQUIRED` の分岐を例に明文化される。
- dashboard 指標と分母の扱いが明文化される。
- 成功内訳 (funnel) を持たない理由が明文化される。
- `stage_attempt` を今回追加しない理由が明文化される。
- 実装時に必要な emit point と分類境界が明文化される。
- 全 `AIProviderStateError` leaf の網羅分類を固定するテスト要件が明文化される。

---

## 1. Metric Contract

### 1.1 Metric

```text
metric: vector.embedding.processing_outcome
type: counter
unit: 1
attributes:
  result = succeeded | failed | infra_error
```

`processing_outcome` は、embedding stage の処理結果を表す。

これは `article_stage` span result の完全ミラーではない。span の `skipped` / `failed` は粗く、冪等 skip・race loss・処理失敗・インフラ失敗を区別できないため、metric は分類が判明する境界で emit する。

### 1.2 Result Vocabulary

#### succeeded

embedding がベクトルを生成し、業務行と成功 audit を同一 transaction で永続化した。処理成功。Stage 5 は終端なので次工程は無い。

curation の `signal`+`noise`、assessment の `in_scope`+`out_of_scope` に当たる「処理成功」を、embedding は単一値で表す。embedding は記事を分類しないため、成功の内訳を持たない。

#### failed

embedding 処理に入ったが、有効なベクトル保存に到達できなかった。かつ、その原因が stage 自身のコード・リクエスト構築・対象内容にある (= 分母に算入すべき処理失敗)。

具体的な初期対象は §2 で確定する。

#### infra_error

embedding 処理ロジック外の、環境・設定・課金・依存先 (DB / AI provider) の状態に起因する失敗。stage のコードを変えずに、インフラ・運用側の回復で直る。

具体的な初期対象は §2 で確定する。`infra_error` は成功率の分母から外すが、count としては可視化し dashboard から消さない。

---

## 2. Failure Classification

本 metric の中心。失敗を `failed` (分母に算入) と `infra_error` (分母外) に分ける規則を定義する。

### 2.1 原則

> **infra_error** = stage のコード・出力を変えずに、環境・設定・課金・依存先の回復で直る失敗。
> **failed** = stage 自身のコード・リクエスト構築・対象内容が原因の失敗。

success rate (§3) が「インフラ健全時に stage のロジック・入力が健全か」を意味するべき、という観点から引く線である。provider 障害や誤設定で success rate が暴落して「embedding stage が壊れた」と誤認させないために、それらは分母外 (`infra_error`) に退避し、別 count として可視化する。

### 2.2 domain の区切りを流用しない理由

provider error には既に 2 つの区切りがあるが、どちらも本 metric の分類軸とは一致しない。

- class 系統 (`AIProviderStateError` / `AIProviderContentError`) は「provider・環境の状態か / 入出力の内容か」を分ける。
- retry 軸 (`AIProviderFailureMode`) は「起きた後どう回復するか (即時再試行 / 待つ / hold / 捨てる)」を分ける。

これらを流用すると `AIProviderRequestInvalidError` を誤分類する。`AIProviderRequestInvalidError` は class 系統では `AIProviderStateError`、retry 軸では `OPERATOR_ACTION_REQUIRED` に属し、`AIProviderConfigurationError` / `AIProviderInsufficientBalanceError` と同じ括りになる。しかし意味は「こちらが provider 仕様に合致しない request を組み立てた」= stage のコード欠陥であり、`failed` に算入すべきである。一方 Configuration / Balance は環境・課金で直る `infra_error` である。

| 具体エラー | class 系統 | retry 軸 (FAILURE_MODE) | 本 metric の bucket |
|---|---|---|---|
| Network | StateError | ATTEMPT_SCOPED | infra_error |
| ServiceUnavailable | StateError | TIME_BASED_RECOVERY | infra_error |
| RateLimited | StateError | TIME_BASED_RECOVERY | infra_error |
| UsageLimitExhausted | StateError | CONDITION_BASED_RECOVERY | infra_error |
| Configuration | StateError | OPERATOR_ACTION_REQUIRED | infra_error |
| InsufficientBalance | StateError | OPERATOR_ACTION_REQUIRED | infra_error |
| **RequestInvalid** | **StateError** | **OPERATOR_ACTION_REQUIRED** | **failed** |
| InputRejected / OutputBlocked | ContentError | TARGET_REJECTED | failed |

`OPERATOR_ACTION_REQUIRED` の内側で分類が割れること、`StateError` 全体を一括で `infra_error` にできないことが、「domain の区切りをそのまま集計に流用しない」決定的な根拠である。本 metric は独自の分類を持つ。

### 2.3 分類表

#### infra_error (分母外、emit する)

```text
SQLAlchemyError                       (DB)
AIProviderNetworkError                (timeout / connection refused / DNS)
AIProviderServiceUnavailableError     (provider 5xx)
AIProviderRateLimitedError            (HTTP 429 / RESOURCE_EXHAUSTED)
AIProviderUsageLimitExhaustedError    (利用枠枯渇)
AIProviderConfigurationError          (API key / model 名 / endpoint 不正)
AIProviderInsufficientBalanceError    (残高不足)
ready_build_failed_db_error           (ready-build 中の SQLAlchemyError)
```

#### failed (分母に算入)

```text
AIProviderRequestInvalidError         (request 構造が provider 仕様に不適合 = stage 欠陥)
AIProviderInputRejectedError          (入力内容の拒否)
AIProviderOutputBlockedError          (出力の safety / recitation block)
EmbeddingResponseInvalidError         (provider 応答が embedding schema に不適合)
catch-all unexpected                  (想定外例外)
ready_build_failed_contract_invalid   (ready-build 中の ValidationError)
ready_build_failed_unexpected_error   (ready-build 中の上記以外)
```

想定外例外を `failed` に倒すのは、エラーハンドリング漏れやコードバグを安易に成功率の分母から除外しないためである。

### 2.4 分類の置き場と網羅性

- provider error の `infra_error` / `failed` 判定は、stage 中立な述語で行う (例: `is_infra_provider_error(exc: AIProviderError) -> bool`)。入力が stage 非依存の provider error なので、配置も stage 中立な consumer / observability 層とし、ドメインエラー class に bucket 属性・メソッドを生やさない。本 PR で consume するのは embedding handler のみ。assessment / curation は将来この同じ述語を adopt できるが、本 PR では書き換えない (別 PR、Non-goals 参照)。
- handler は marker 型 (Terminal / Recoverable) では分類を決めない。Terminal arm には content 拒否 (failed) と Configuration / Balance (infra_error) が、Recoverable arm には Network 等 (infra_error) と `EmbeddingResponseInvalidError` (failed) が混在するため、`exc.provider_error` を共有述語に渡して bucket を決める。`provider_error` が `None` の marker (`EmbeddingResponseInvalidError`) は `failed`。
- 全 `AIProviderStateError` leaf が `infra_error` / `failed` のどちらかに明示分類されることを網羅テストで固定する (§6.6)。新しい provider error subclass を分類し忘れたら CI で落ちる。未分類は `failed` を default とする (未知の失敗を分母に出して気づけるようにし、silent に分母外へ隠さない)。

---

## 3. Excluded Outcomes

以下は `vector.embedding.processing_outcome` に emit しない。

```text
rate_limited (gate skip)
ANALYZED_ARTICLE_MISSING
ALREADY_EMBEDDED
race loss
```

### 3.1 rate_limited (gate skip)

rate limit **gate** による事前 skip は処理品質ではなく capacity 制御である。既存の `vector.analysis.rate_limit_gate_skipped{stage=embedding}` でも観測できるため、初期 metric では emit しない。

これは `AIProviderRateLimitedError` (呼び出し中に provider から返る 429) とは別物である。後者は処理試行が実際に provider へ到達して throttling に当たった infra 失敗であり、`infra_error` に算入する (§2.3)。前者は試行に入る前の skip なので emit しない。

### 3.2 Ready-build Blocked (全コード)

`ANALYZED_ARTICLE_MISSING` は上流 analyzed article が存在しない stale、`ALREADY_EMBEDDED` は冪等・重複実行の揺れである。いずれも処理成功率の分母に混ぜない。

なお audit (`pipeline_events`) 上はこれらを `REJECTED` で記録するが、これは「処理試行の結末」ではないため `processing_outcome` には emit しない。span result も `skipped` であり `rejected` ではない。embedding は内容起因の reject 経路を持たないため、metric vocabulary に `rejected` を設けない。

### 3.3 Race Loss

楽観ロック敗北 (save が `False`) は重複実行の揺れであり、commit に到達しないため処理成功でも失敗でもない。分母に混ぜない。

---

## 4. Dashboard Metrics

指定 window 内の `vector.embedding.processing_outcome` を `result` 別に集計する。

### 4.1 Counts

```text
succeeded_count   = count(result = succeeded)
failed_count      = count(result = failed)
infra_error_count = count(result = infra_error)
```

### 4.2 Embedding Success Percent

```text
embedding_success_percent =
  100 * succeeded_count
  / NULLIF(succeeded_count + failed_count, 0)
```

意味:

embedding の処理試行が、インフラ・運用障害を除いて有効なベクトル保存に到達した割合。

`infra_error` は分母に入れない。インフラ・運用障害は処理品質ではないため成功率を汚さないが、`infra_error_count` として別に見る。

`infra_error_count` は全インフラ失敗の総数ではない。Redis / queue / gate 例外や timeout のように task ごと落ちる失敗は、初期実装では `execute/generate_embedding` span の ERROR 側で見る。

### 4.3 No Funnel Metric

embedding は記事を分類しないため、assessment の `in_scope_share_percent` に相当する funnel 指標を持たない。成功は単一の `succeeded` であり、内訳を割らない。

### 4.4 Initial Dashboard

初期ダッシュボードでは以下だけを表示する。

```text
embedding_success_percent
succeeded_count
failed_count
infra_error_count
```

---

## 5. Emit Policy

### 5.1 Not Span-shadow

`vector.embedding.processing_outcome` は `EmbeddingStageSpan.set_result()` から一律 emit しない。

理由:

- span result `skipped` は ready-build blocked, race loss を区別できない。
- span result `failed` は AI / parse / provider 失敗、DB エラー、backstop 失敗を区別できない。
- 今回の指標は span の見た目ではなく、処理成功率の意味論 (§2 の分類) に合わせる必要がある。

### 5.2 Emit Points

assessment と同じく **3 境界**で emit する。embedding は成功が単一値なので成功境界は `succeeded` だけを emit する。

#### succeeded

`EmbeddingService.execute()` がベクトルを保存し、成功 audit と同一 transaction を commit した後に emit する (既存の `set_embedding_stage_result("succeeded")` 呼び出しの隣)。

race loss で短絡する場合 (`set_embedding_stage_result("skipped")`) は emit しない。

#### failed / infra_error (handler)

`EmbeddingFailureHandler.handle()` の各 match arm が、副作用 (`_audit_failure` / `_audit_unexpected_failure`) より先に emit する。

```text
EmbeddingTerminalError(exc)     -> infra_error if is_infra_provider_error(exc.provider_error) else failed
EmbeddingRecoverableError(exc)  -> infra_error if is_infra_provider_error(exc.provider_error) else failed
SQLAlchemyError                 -> infra_error
catch-all unexpected            -> failed
```

`exc.provider_error` が `None` の marker (`EmbeddingResponseInvalidError` など) は infra 述語が `False` を返し `failed` になる。

handler は分類を共有 `FailureHandlingDecision` に載せず、自身で emit する (durability 境界の所有者が出す)。

#### failed / infra_error (ready-build failed)

ready-build 中の blocked 以外の例外は、`project_ready_build_failure(stage_prefix="embedding", exc=exc)` に基づき分類する。

```text
ready_build_failed_db_error          -> infra_error
ready_build_failed_contract_invalid  -> failed
ready_build_failed_unexpected_error  -> failed
```

`generate_embedding` の ready-build `except Exception` 節内で projection 分類し、再送出前に emit する。audit (`_append_ready_build_failed_audit`) は best-effort であり、audit drop は metric emit を抑止しない。再送出は span backstop に到達し span result を `failed` にするが、backstop は `processing_outcome` を emit しないため二重計上しない。

### 5.3 Backstop Does Not Emit

context manager backstop の `failed` だけでは `processing_outcome` を emit しない。backstop は span 可視性の安全網であり、processing outcome の分類根拠ではない。

これにより、分類境界をすり抜けて backstop だけに到達する失敗 (gate の Redis 例外、timeout の CancelledError、その他 BaseException) は `processing_outcome` に計上されない。これらは `execute/generate_embedding` span の ERROR ステータスで観測する設計境界とする (curation / assessment と同じ方針)。

---

## 6. Stage Attempt Counter

今回は `vector.embedding.stage_attempt` counter は追加しない。

理由:

- task が例外で落ちたかどうかは `execute/generate_embedding` span の ERROR で既に見られる。
- 今回の主目的は stage 実行の信頼性ではなく、embedding 処理成功率である。
- handled な DB / provider インフラ失敗は `processing_outcome{result=infra_error}` として可視化するため、dashboard から消えない。
- metrics テーブル統一や sampling 耐性が必要になった時点で、別途 `stage_attempt` を検討すればよい。

---

## 7. Test Requirements

curation / assessment と同じハーネス分担に倣う。helper は `tests/logfire/_metric_helpers.py` を再利用する。

### 7.1 Metric Emit

- ベクトル保存 + audit commit 後に `processing_outcome{result=succeeded}` が +1 される。
- handler の各 marker で `processing_outcome` が分類表 (§2.3) どおり emit される。
  - `AIProviderNetworkError`, `AIProviderServiceUnavailableError`, `AIProviderRateLimitedError`, `AIProviderUsageLimitExhaustedError`, `AIProviderConfigurationError`, `AIProviderInsufficientBalanceError` を詰めた marker -> `infra_error`。
  - `AIProviderRequestInvalidError`, `AIProviderInputRejectedError`, `AIProviderOutputBlockedError` を詰めた marker, `EmbeddingResponseInvalidError`, catch-all -> `failed`。
  - `SQLAlchemyError` -> `infra_error`。
- ready-build SQLAlchemyError で `infra_error`、ValidationError / その他で `failed` が +1 される。
- 各 path は割り当て外の result を一切 emit しない (vocabulary 排他、3 値全て検証)。

### 7.2 Non-emitted Cases

- `rate_limited` (gate skip) は emit されない。
- `ANALYZED_ARTICLE_MISSING` は emit されない。
- `ALREADY_EMBEDDED` は emit されない。
- race loss (save が False) は emit されない。

### 7.3 Attribute Safety

- data point attributes は `{"result": <value>}` のみ。
- metric dump に `analyzed_article_id`, `analyzable_article_id`, `source_id`, URL, prompt, raw response, error message, model, vector dimension, failure_kind が混入しない。

### 7.4 Backstop

- context manager backstop の `failed` だけでは `processing_outcome` を emit しない。
- backstop は span 可視性の安全網であり、processing outcome の分類根拠ではない。

### 7.5 Emit Independence From Audit

§5.2 の「分類は match 時点で確定し副作用より先に emit する / audit drop は emit を抑止しない」を固定する。

- handler の `_audit_failure` / `_audit_unexpected_failure` が DB 失敗 (audit drop) しても、`failed` / `infra_error` は emit される。
- ready-build failed の audit (`_append_ready_build_failed_audit`) が drop されても、`failed` / `infra_error` は emit される。

### 7.6 Classification Exhaustiveness

§2.4 の網羅性を固定する。

- 全 `AIProviderStateError` の具象 leaf が `is_infra_provider_error` で `True` / `False` のどちらかに明示分類される (分類表 §2.3 と一致)。
- 新しい `AIProviderStateError` subclass を追加して分類に載せ忘れたら、本テストが落ちる (silent な default 化を防ぐ)。
