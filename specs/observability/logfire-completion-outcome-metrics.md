# Logfire Completion Processing Outcome Metrics

作成: 2026-06-17
Status: Implemented (PR #819)

関連:
- [`logfire-curation-outcome-metrics.md`](./logfire-curation-outcome-metrics.md) (curation 先行例、PR #814)
- [`logfire-assessment-outcome-metrics.md`](./logfire-assessment-outcome-metrics.md) (assessment 同型先行例、PR #816)
- [`logfire-embedding-outcome-metrics.md`](./logfire-embedding-outcome-metrics.md) (embedding 先行例、PR #817。provider 失敗を health 軸で分類する設計の直接の親)

---

## Work Definition

### Problem

completion stage (Stage 2 / 本文補完・scraping) について、Logfire 上で「インフラ・一時障害に汚されない処理成功率」を可視化する。

見たいのは「ready-build から本文取得・本文抽出・Article 永続化までを含めた処理としての成功率」である。ただし DB / ネットワーク / 相手サーバーの一時障害など、stage のコードでも対象コンテンツでもない一時的な失敗は処理品質とは別なので、成功率の分母から外す。curation / assessment / embedding で確立した `processing_outcome` パターンを completion へ展開する。

ただし completion は analysis BC の 3 stage と構造が大きく異なり、metric 設計を変える。

1. **判定スプリットが無い**。completion は記事を分類せず、本文を取得して `AnalyzableArticle` に昇格させるだけなので、`in_scope` / `out_of_scope` のような成功の内訳が無い。成功は単一の `succeeded` で、funnel 指標を持たない (embedding と同じ)。
2. **失敗が provider error ではなく scraping/HTTP 系**。失敗分類の素材は `ExternalFetchError` family (HTTP status / transport) と content 抽出失敗 (`ScrapeContentFailure`) であり、`AIProviderError` ではない。`infra_error` / `failed` の線は HTTP の性質とコンテンツの可否で引く (本 spec の中心。§2)。
3. **`article_stage` span が無い**。analysis 3 stage は task 冒頭で `article_stage` span を開くが、completion は collection BC にあり span 系列に乗っていない。本 metric は span を新設せず counter のみを足す (§6)。
4. **claim ベースの DB 駆動 retry**。completion は cron poller + lease で 1 記事を一時失敗のたびに再 scrape する。失敗 handler は状態遷移と audit を**同一 transaction で atomic** に行い、claim 喪失 (`updated == False`) を自前で検出する。emit はこの構造に合わせる (§5)。

### Evidence

- `scrape_html_body` task は `ReadyForArticleCompletion.try_advance_from()` で入力 VO を構築してから `ArticleCompletionService.execute()` に進む。Stage 2 成功時は `curate_content` を chain firing する。taskiq retry は持たず (`max_retries=0`)、再投入は cron poller (`dispatch_html_fetch_jobs`) が DB の `ready_at` を SSoT として行う。
- ready-build 失敗は typed `ArticleCompletionReadyBuildError` で表現され、`EVENT_TYPE` を持つ。`IncompleteArticleMissing` / `IncompleteArticleNotRunning` はいずれも `EventType.SKIPPED` (precondition 由来の stale / 冪等)。VO 層 error (`CanonicalArticleUrlInvalid` / `ObservedArticleInvalid` / `SourceNotRegistered`) は ready が翻訳せず素通しし、task の `except Exception` 節が拾う。
- `ArticleCompletionService.execute()` は scrape → complete → persist を orchestration する。scrape / complete の失敗は `ArticleCompletionFailureHandler` が状態遷移と audit を完了させて `None` を返す。persist は成功 / race-loss を同一 tx、DB 例外は別 tx で audit して再 raise する。
- persist の結末は `CompletionSucceeded` / `CompletionSuperseded` (claim を別 worker に追い越された) / `CompletionUrlConflict` (同一 URL 衝突) の 3 値。
- scrape 失敗の値は `ScrapeFailure = ExternalFetchError | ScrapeContentFailure`。`ExternalFetchError` は各 leaf に `retryable: ClassVar[bool]` を持ち、module docstring がこれを「再実行で結果が変わりうるか=失敗の性質」の SSoT と宣言する (scheduling ではない)。content 失敗 (`ScrapeNotHtml` / `ScrapeParserGaveUp` / `ScrapeParseCrashed` / `ScrapeContentQualityTooLow`) は応答を得たが使える本文でなかった観測値で、常に terminal。
- `classify_scrape_failure()` は `ScrapeFailure` を `ScrapeTerminal | ScrapeRetryable` に写像するが、これは backoff schedule (BLIP / OUTAGE / TIMEOUT / UNKNOWN) と `max_attempts` を畳んだ **handling / scheduling 軸**であり、本 metric の health 軸とは別物である。
- `ArticleCompletionFailureHandler` の各経路 (`_handle_terminal` / `_handle_temporary` / `handle_completion_rejected`) は、`close_claimed` / `schedule_retry` の戻り `updated` で claim 喪失を検出し、`updated == False` のとき `append_stale_attempt` で記録して何もせず返る。状態遷移と audit は同一 tx・commit で atomic。`handle_persist_crashed` のみ別 session の best-effort audit。
- completion には AI quota / rate-limit gate が無い (scrape 自体が外部 I/O)。embedding の `rate_limited` gate skip に相当する除外カテゴリは存在しない。
- ready-build 失敗の分類は completion 専用 projector `_project_ready_build_error()` (`app/audit/stages/completion.py`) が SSoT。`ArticleCompletionReadyBuildError` は `EVENT_TYPE` / `FAILURE_KIND`、`CanonicalArticleUrlInvalidError`→`url_invalid`、`ObservedArticleInvalidError`→`observed_article_invalid`、`SourceNotRegisteredError`→`source_not_registered`、`SQLAlchemyError`→`db_error`、他→`unexpected_error` に分け、VO error はすべて `EventType.FAILED`。共有 `project_ready_build_failure()` (`ValidationError`→`contract_invalid`) は embedding / curation / assessment が使うもので、completion は使わない。
- taskiq の OTel middleware が `execute/scrape_html_body` span を自動で作るため、task が例外で落ちたかどうかは既存 span から観測できる。

### Invariants

- 本 metric は処理試行単位で集計する (記事単位の最終成功率ではない)。一時失敗で retry される記事は試行のたびに emit する (§5.2)。
- `succeeded` は completion 処理成功として扱う (本文を取得し `AnalyzableArticle` を永続化した)。
- `failed` は completion 処理成功率の分母に含める。
- `infra_error` は emit するが、completion 処理成功率の分母には含めない。
- claim 喪失 (race-loss: `CompletionSuperseded`, handler の `updated == False`)、`CompletionUrlConflict`、ready-build blocked (`EventType.SKIPPED`) は成功率の分母から除外する。
- **失敗の `infra_error` / `failed` 分類は、handling / scheduling 軸 (`ScrapeTerminal` / `ScrapeRetryable`、backoff schedule) を流用しない。失敗の性質 (一時的か / 恒久的か) で分類する (§2)。**
- transport 失敗の性質判定は `ExternalFetchError.retryable` (失敗性質の SSoT) に委譲し、metric 側で 18 leaf を再列挙しない。content 失敗は常に `failed` (応答を得たうえで使えなかった、コンテンツ側の恒久失敗)。
- 分類の置き場は consumer (metric / handler / task) 側の明示分類とする。ドメインエラー class に集計 bucket を属性・メソッドとして持たせない。
- content 失敗の分類は閉じ union を明示 match + `assert_never` で総当たりする。新 content variant は型検査と totality テスト (§7.6) で分類漏れを落とし、silent に `failed` へ流さない。
- `infra_error` は全インフラ失敗の総数ではなく、この metric の分類境界で infra と断定できる handled/classified failure だけを表す。
- metric attribute に `incomplete_article_id`, `analyzable_article_id`, `source_id`, source 名, URL, HTTP status, reason code, body sample, error message, failure_kind は載せない。`result` のみ。
- `vector.completion.processing_outcome` は span-shadow ではない (そもそも completion に stage span は無い)。分類が判明する task / service / handler 境界で emit する。
- handler の失敗 emit は、状態遷移 tx が commit し、かつ claim が自分のものだった (`updated == True`) ときにのみ行う。これは completion の「状態遷移と audit を atomic に行い claim 喪失を検出する」構造に合わせた、embedding (best-effort audit の前に emit) との意図的な相違である (§5.2)。

### Non-goals

- 成功の内訳 (分類スプリット) / funnel 指標は持たない。completion は記事を分類しない。
- HTTP status 別・reason 別・source 別 breakdown は扱わない (分類は `result` の 3 値に畳む)。
- `failure_kind` label は追加しない。
- `completion_stage_span` (analysis の `article_stage` span 相当) は新設しない (§6)。Stage 2 を span 系列に可視化するのは別作業。
- `stage_attempt` counter は追加しない。
- `pipeline_events` schema、`ScrapeTerminal` / `ScrapeRetryable` / `ExternalFetchError` taxonomy、retry policy は変更しない。
- acquisition (#805 `vector.acquisition.outcome`) / curation (#814) / assessment (#816) / embedding (#817) の既存 metric は変更しない。
- timeout / CancelledError / その他 BaseException で task ごと落ちる失敗の infra_error 化はしない (`execute/scrape_html_body` span の ERROR で観測)。

### Done

- Logfire metric `vector.completion.processing_outcome{result}` の仕様が定義される。
- `result` attribute は `succeeded`, `failed`, `infra_error` の 3 値だけを持つ。
- 失敗分類規則 (`infra_error` vs `failed`) と、その背後の原則 (一時的 / 恒久的) が明文化される。
- transport を `ExternalFetchError.retryable` に委譲し、scheduling 軸 (`ScrapeTerminal` / `ScrapeRetryable`) を流用しない理由が明文化される。
- 除外カテゴリ (claim 喪失 / UrlConflict / ready-build blocked) と分母の扱いが明文化される。
- 一時失敗を毎試行 emit する帰結 (retry 多発記事・exhausted 記事が成功率に与える影響) が明文化される。
- span を新設しない理由、`stage_attempt` を追加しない理由が明文化される。
- 実装時に必要な emit point と分類境界が明文化される。
- `ScrapeContentFailure` の網羅分類を固定するテスト要件が明文化される。

---

## 1. Metric Contract

### 1.1 Metric

```text
metric: vector.completion.processing_outcome
type: counter
unit: 1
attributes:
  result = succeeded | failed | infra_error
```

`processing_outcome` は、completion stage の処理試行の結末を表す。completion には `article_stage` span が無いため、これは span result のミラーではなく、分類が判明する境界で独立に emit する counter である。

### 1.2 Result Vocabulary

#### succeeded

本文を取得・抽出し、`AnalyzableArticle` を業務行と成功 audit とともに同一 transaction で永続化した。処理成功。成功時は `curate_content` を chain する。

curation の `signal`+`noise`、assessment の `in_scope`+`out_of_scope` に当たる「処理成功」を、completion は単一値で表す。記事を分類しないため成功の内訳を持たない。

#### failed

completion 処理に入ったが有効な `AnalyzableArticle` 永続化に到達できず、かつその原因が**そのサイト / URL / コンテンツでは恒久的に成功しない**もの (= 分母に算入すべき処理失敗)。

具体的な初期対象は §2 で確定する (paywall / 404 / robots 拒否 / 本文抽出不能 / ドメイン棄却 など)。

#### infra_error

completion 処理ロジック外の、**一時的**な失敗。ネットワーク不通・相手サーバーダウン (5xx) ・通信 timeout・rate limit・DB 障害など、stage のコードを変えずに時間や運用側の回復で直りうるもの。

具体的な初期対象は §2 で確定する。`infra_error` は成功率の分母から外すが、count としては可視化し dashboard から消さない。

---

## 2. Failure Classification

本 metric の中心。失敗を `failed` (分母に算入) と `infra_error` (分母外) に分ける規則を定義する。

### 2.1 原則

> **infra_error** = 一時的な失敗。再実行で結果が変わりうる (ネットワーク・相手サーバー・通信・DB の一時障害)。
> **failed** = 恒久的な失敗。そのサイト / URL / コンテンツでは再実行しても成功しない (アクセス拒否・不在・robots 拒否・本文抽出不能・ドメイン棄却)。

success rate (§4) が「インフラ・一時障害が無いときに、対象を本文付き記事へ昇格できる割合」を意味するべき、という観点から引く線である。相手サーバーの一時ダウンやこちらのネットワーク障害で success rate が暴落して「completion stage が壊れた」と誤認させないために、それらは分母外 (`infra_error`) に退避し、別 count として可視化する。

### 2.2 scheduling 軸を流用しない / 性質 SSoT は再利用する

completion には失敗に関する区切りが既に 2 つあるが、本 metric が使えるのは一方だけである。

- **`ExternalFetchError.retryable` (失敗性質の SSoT)** — 「再実行で結果が変わりうるか=失敗の性質」を表す stage 非依存の bool。これは §2.1 の一時的 / 恒久的の軸と**そのまま一致する**ため、transport 失敗の分類はこれに委譲する。metric 側で leaf を再列挙しない (列挙は性質 SSoT の重複になり drift する)。委譲ゆえ leaf roster の改変に強い: #818 が fallback leaf を 1 つ削除し 2 つ (server=retryable / client=terminal) に分割したが、述語は `retryable` を読むだけなので変更不要だった (§2.3 の文書表のみ追従)。
- **`ScrapeTerminal` / `ScrapeRetryable` (handling / scheduling 軸)** — `classify_scrape_failure()` が返す、backoff schedule と `max_attempts` を畳んだ「いつ・どう retry するか」の決定。これは性質ではなく handling なので**流用しない**。metric は下層の `retryable` を直接読む。

embedding (#817) では health 軸に相当する単一 bool が provider error に無く、`OPERATOR_ACTION_REQUIRED` の内側で infra と failed が割れたため全 leaf を明示列挙する必要があった。completion では `ExternalFetchError.retryable` が health 軸と一致するため、transport は列挙不要でこれに委譲できる。これが embedding との設計差である。

### 2.3 分類表

#### infra_error (分母外、emit する)

```text
# transport: ExternalFetchError.retryable == True に委譲
FetchRateLimitedError          (HTTP 429)
FetchOriginServerError         (HTTP 500 / 503 — 相手サーバーダウン)
FetchGatewayError              (HTTP 502 / 504)
FetchRequestTimeoutError       (HTTP 408)
FetchRetryableStatusError      (HTTP 425 等)
FetchUnexpectedServerStatusError  (未マップ 5xx fallback、retryable)
FetchTimeoutError              (connect / read timeout)
FetchNetworkError              (DNS / TCP / TLS 失敗、未知 transport 例外の保守的 fallback)

# DB
SQLAlchemyError                (persist crash の DB 例外)
ready_build_failed_db_error    (ready-build 中の SQLAlchemyError)
```

#### failed (分母に算入)

```text
# transport: ExternalFetchError.retryable == False に委譲
FetchAccessDeniedError         (HTTP 401 / 403 — paywall / forbidden)
FetchLegalBlockError           (HTTP 451)
FetchResourceNotFoundError     (HTTP 404 / 410)
FetchUnexpectedClientStatusError  (未マップ 4xx / 1xx / 範囲外 fallback、terminal。#818 是正で旧 retryable 扱いから failed へ)
FetchSsrfBlockedError          (private IP 等、その URL は恒久的に取得しない)
FetchRobotsDisallowedError     (robots.txt の明示 Disallow)
FetchRobotsUnavailableError    (robots.txt 取得不能 → 取得不可扱い)
FetchRedirectBlockedError      (redirect 追従しない policy。未マップ 3xx もここに倒れる)
FetchRedirectLoopError         (redirect loop / 回数超過)
FetchResponseTooLargeError     (サイズ超過)
FetchContentTypeMismatchError  (Content-Type 不一致)

# content (応答は得たが使える本文でなかった、常に failed)
ScrapeNotHtml
ScrapeParserGaveUp
ScrapeParseCrashed
ScrapeContentQualityTooLow

# domain
CompletionRejection            (本文は揃ったが AnalyzableArticle にできない)

# ready-build: EventType.FAILED かつ DB 以外 (completion 専用 projector の分類)
CanonicalArticleUrlInvalidError    (source_url が壊れている / url_invalid)
ObservedArticleInvalidError        (observed_article が壊れている / observed_article_invalid)
SourceNotRegisteredError           (source 未登録 / source_not_registered)
ArticleCompletionReadyBuildError(EventType.FAILED)  (typed ready-build failed、現状未使用)
ready-build unexpected_error       (上記・DB 以外の想定外)

# persist: 非 DB の想定外
persist crash の非 SQLAlchemyError  (想定外)
```

`infra_error` の `SQLAlchemyError` / `*_db_error` は、emit point を持つ境界 (persist crash・ready-build) の DB 例外に限る。失敗 handler の atomic-tx (`_handle_terminal` / `_handle_temporary` / `handle_completion_rejected`) 内で起きる commit 失敗は emit せず task へ貫通する (§5.4)。「DB 障害はすべて infra_error」ではない。

content 失敗を一律 `failed` にするのは、応答 (HTTP) は得られており、失敗の所在が**取得対象コンテンツ側**にあるためである。一時障害ではないので分母に算入する。

想定外例外を `failed` に倒すのは、エラーハンドリング漏れやコードバグを安易に成功率の分母から除外しないためである。

### 2.4 分類の置き場と網羅性

- scrape 失敗の `infra_error` / `failed` 判定は、collection BC 内の consumer 層に置く stage 中立な述語で行う (例: `is_infra_scrape_failure(failure: ScrapeFailure) -> bool`)。入力が `ScrapeFailure` (`ExternalFetchError | ScrapeContentFailure`) なので、配置は completion BC の observability 層 (例: `app/collection/article_completion/outcome.py` 新規) とし、ドメインエラー class や `ScrapeFailure` VO に表示用bucketを生やさない。

  ```python
  def is_infra_scrape_failure(failure: ScrapeFailure) -> bool:
      # transport は失敗性質の SSoT (retryable) に委譲。
      if isinstance(failure, ExternalFetchError):
          return failure.retryable
      # content は閉じ union を明示 match し、未分類は assert_never で型/実行時に落とす。
      match failure:
          case (
              ScrapeNotHtml()
              | ScrapeParserGaveUp()
              | ScrapeParseCrashed()
              | ScrapeContentQualityTooLow()
          ):
              return False
          case _:
              assert_never(failure)
  ```

- transport は `ExternalFetchError.retryable` に委譲するため、新しい transport leaf は自身の `retryable` で自動分類される (metric 側の更新不要)。content は閉じ union なので、述語が全 variant を明示 match し `case _: assert_never(failure)` で締める (`classify_scrape_failure` 既存の totality guard と同じ方針)。新 content variant を `ScrapeContentFailure` に足すと型検査が `assert_never` で落ち、分類を載せるまで気づける (silent な `failed` 落ちを防ぐ)。§7.6 の totality テストがこの締めと union メンバ集合を固定する。
- scrape 以外の境界は述語を介さず直接分類する。`CompletionRejection` → `failed`、persist の DB 例外 → `infra_error` (非 DB は `failed`)、ready-build → §5.2。

---

## 3. Excluded Outcomes

以下は `vector.completion.processing_outcome` に emit しない。

```text
ready-build blocked (EventType.SKIPPED)   # IncompleteArticleMissing / NotRunning
CompletionSuperseded                       # persist claim 喪失 (race-loss)
CompletionUrlConflict                      # 同一 URL 衝突
handler stale attempt (updated == False)   # 失敗 handling 中の claim 喪失 (race-loss)
```

### 3.1 Ready-build Blocked

`IncompleteArticleMissing` (行が消えた) / `IncompleteArticleNotRunning` (既に別経路で処理済み) は、cron poller が claim した後に状況が変わった陳腐化である (`EventType.SKIPPED`)。処理試行の結末ではないため分母に混ぜない。audit には `SKIPPED` で記録されるが metric には emit しない。

### 3.2 Claim 喪失 (race-loss)

completion は複数 worker / lease 失効 + 再 dispatch で同一行を二重に拾いうる。`CompletionSuperseded` (persist 時に claim を別 worker に追い越された) と、失敗 handler の `updated == False` (`close_claimed` / `schedule_retry` が 0 行更新 = claim 喪失) は、いずれも「この試行はもう自分のものではない」race-loss である。別 worker 側が同じ記事の結末を emit するため、ここで emit すると同一記事の二重計上で成功率が汚れる。よって emit しない。

### 3.3 CompletionUrlConflict

保存時に同一 canonical URL が既存だった衝突は、scrape の成否ではなく dedup の揺れである。処理成功でも処理失敗でもないため、race-loss と同様 emit しない。

### 3.4 No Rate-limit Gate

completion には AI quota / rate-limit gate が無い (scrape 自体が外部 I/O)。embedding / curation の `rate_limited` (gate skip) に相当する除外カテゴリは存在しない。なお呼び出し中に相手から返る HTTP 429 (`FetchRateLimitedError`) は別物で、試行が実際に相手へ到達した一時障害なので `infra_error` に算入する (§2.3)。

---

## 4. Dashboard Metrics

指定 window 内の `vector.completion.processing_outcome` を `result` 別に集計する。

### 4.1 Counts

```text
succeeded_count   = count(result = succeeded)
failed_count      = count(result = failed)
infra_error_count = count(result = infra_error)
```

### 4.2 Completion Success Percent

```text
completion_success_percent =
  100 * succeeded_count
  / NULLIF(succeeded_count + failed_count, 0)
```

意味:

completion の処理試行が、インフラ・一時障害を除いて本文付き記事の永続化に到達した割合。`infra_error` は分母に入れない。

### 4.3 一時失敗を毎試行 emit する帰結

一時障害 (`infra_error`) は retry のたびに毎試行 emit する。帰結として:

- 3 回一時失敗してから成功した記事は `infra_error × 3 + succeeded × 1` を emit する。`infra_error` は分母外なので `completion_success_percent` は汚れないが、`infra_error_count` は試行数で膨らむ。これは「相手サーバー・ネットワークの不調の発生量」を測る量として意図どおりである (記事数ではない)。
- 一時失敗だけを繰り返して retry 上限で打ち切られた (exhausted) 記事は、性質が一時的なまま `infra_error` を emit し、`failed` / `succeeded` を一切 emit しない。つまり**恒久的な成否の判定に至らなかった記事は成功率の分母に現れない**。`completion_success_percent` は「最終的に恒久判定 (成功 or 恒久失敗) に達した試行のうち成功した割合」を意味し、一時障害で決着しなかった試行はカバレッジ側 (`infra_error_count`) で見る。

### 4.4 No Funnel Metric

completion は記事を分類しないため funnel 指標を持たない。成功は単一の `succeeded`。

### 4.5 Initial Dashboard

```text
completion_success_percent
succeeded_count
failed_count
infra_error_count
```

---

## 5. Emit Policy

### 5.1 Not Span-shadow

completion には `article_stage` span が無いため、本 metric は span result から自動 emit されない。分類が判明する task / service / handler 境界で明示 emit する。

### 5.2 Emit Points

| 境界 | 場所 | emit 条件 | result |
|---|---|---|---|
| 成功 | `ArticleCompletionService.execute()` の `CompletionSucceeded` arm (persist commit 後) | — | `succeeded` |
| scrape 失敗 | `ArticleCompletionFailureHandler._handle_terminal` / `_handle_temporary` (状態遷移 commit 後、`updated == True`) | `is_infra_scrape_failure(failure)` | `True`→`infra_error` / `False`→`failed` |
| complete 棄却 | `ArticleCompletionFailureHandler.handle_completion_rejected` (commit 後、`updated == True`) | — | `failed` |
| persist crash | `ArticleCompletionFailureHandler.handle_persist_crashed` (best-effort audit の前) | `isinstance(exc, SQLAlchemyError)` | `True`→`infra_error` / `False`→`failed` |
| ready-build failed | `scrape_html_body` task の except 節 (再 raise 前) | §5.2 ready-build 参照 | `skipped`→非emit / それ以外 |

#### succeeded

`execute()` が persist tx を commit した後、`CompletionSucceeded` arm で emit する。`CompletionSuperseded` / `CompletionUrlConflict` arm では emit しない (§3)。`CompletionSucceeded` は `_delete_claimed` が自分の claim を確定したうえでの成功なので、claim 喪失 gate は不要。

#### scrape 失敗 / complete 棄却 (handler)

`_handle_terminal` / `_handle_temporary` / `handle_completion_rejected` は、状態遷移 (`close_claimed` / `schedule_retry`) と audit を**同一 tx で commit** した後、`updated == True` のときにのみ emit する。

- `updated == False` (claim 喪失) は race-loss として emit しない (§3.2)。
- 分類は scrape 失敗が `is_infra_scrape_failure(failure)`、complete 棄却が一律 `failed`。retry / exhausted の別 (`_handle_temporary` の `is_exhausted`) は emit する result を変えない (性質ベース。§4.3)。

この「状態遷移 commit 後・`updated` gate 後に emit」は、completion の atomic-tx + claim-revalidation 構造に合わせたもので、embedding の「best-effort audit の前に emit」とは意図的に異なる。completion では状態遷移と audit が同一 tx で不可分なので、tx が rollback した試行 (= 状態が遷移していない = lease 失効で再試行される) を emit すると過大計上になる。

#### persist crash (handler)

`handle_persist_crashed` は別 session の best-effort audit の**前**に emit する (audit drop が emit を抑止しないため)。`SQLAlchemyError` は `infra_error`、それ以外の想定外は `failed`。claim gate は無い (persist tx は既に crash しており、DB 障害そのものを infra として計上する)。

#### ready-build failed (task)

`scrape_html_body` の ready-build 例外節で、再 raise 前に分類して emit する。task は既に `exc.EVENT_TYPE` で分岐しているため、metric は EVENT_TYPE と `SQLAlchemyError` の直読で足りる。

```text
except ArticleCompletionReadyBuildError as exc:   # task は既に exc.EVENT_TYPE で分岐
    EventType.SKIPPED  -> 非emit (blocked、§3.1)
    EventType.FAILED   -> failed
except Exception as exc:
    isinstance(exc, SQLAlchemyError) -> infra_error
    それ以外 (CanonicalArticleUrlInvalid / ObservedArticleInvalid /
              SourceNotRegistered / 想定外)  -> failed
```

完成形を持つのは completion 専用 projector `_project_ready_build_error()` (Evidence) で、metric の infra/failed はその分類と一致する (唯一の infra は `db_error` = `SQLAlchemyError`、VO error / 想定外はすべて `failed`)。共有 `project_ready_build_failure()` は VO error を `unexpected_error` に丸めるため completion では使わない。metric は infra/failed の 2 値しか要らないので audit projector を import せず emit 点で直読し、粗い集計分類はconsumer側に置く。

best-effort audit (`_append_ready_build_error_audit`) は自前で例外を握るため、audit drop は emit を抑止しない。再 raise は taskiq の `execute/scrape_html_body` span を ERROR にするが、span は `processing_outcome` を emit しないため二重計上しない。

### 5.3 Backstop / 想定外 escape

completion には stage span backstop が無い。分類境界をすり抜けて task を貫通する失敗 (timeout の CancelledError、gate 不在ゆえ該当は少ないが想定外の BaseException 等) は `processing_outcome` に計上せず、`execute/scrape_html_body` span の ERROR ステータスで観測する (analysis 3 stage と同じ方針)。

### 5.4 失敗 handler の atomic-tx commit 失敗は emit しない

`_handle_terminal` / `_handle_temporary` / `handle_completion_rejected` は、状態遷移 (`close_claimed` / `schedule_retry`) と audit を同一 tx で commit したうえで emit する (§5.2)。この tx 内 (close/schedule・audit append・commit のいずれか) で DB 障害が起きると、例外は emit に到達する前に handler を抜ける。これらの handler は `ArticleCompletionService.execute()` の try/except 外 (scrape / complete 失敗経路、`service.py` の `handle_scrape_failure` / `handle_completion_rejected` 呼び出し) で呼ばれるため、例外は `persist_crashed` に**到達せず** task まで貫通する。

この場合 `processing_outcome` は emit しない (状態が遷移しておらず、lease 失効で sweep → 再 dispatch され再試行される。emit すると非永続な試行を過大計上する)。失敗は `execute/scrape_html_body` span の ERROR で観測する。

つまり本 metric の `infra_error` が拾う DB 障害は、専用 emit 点を持つ persist crash (§5.2 persist crash) と ready-build DB (§5.2 ready-build failed) に限られる。失敗 handler tx の commit 失敗はこの metric の対象外で、span ERROR が担う。これは「失敗を隠さない」一方で「決着していない試行を成功率に混ぜない」ための意図的な線引きである。

---

## 6. No Stage Span / No Stage Attempt Counter

### 6.1 span を新設しない

analysis 3 stage の `article_stage` span (記事 1 本の工程通過記録) に相当する `completion_stage_span` は本 PR で新設しない。

理由:

- 本 metric は span の影ではなく独立 counter なので、可視化目的 (成功率) は span 無しで達成できる。
- completion の実行モデル (cron poller + lease で 1 記事を一時失敗のたびに再 scrape) は「1 task = 1 記事 1 回通過」を前提とする `article_stage` span 系列に素直に乗らない。乗せるなら別設計を要する。
- 記事 1 本を completion 工程で trace したい具体的 consumer が現状いない (YAGNI)。
- 想定外 escape は taskiq の `execute/scrape_html_body` span で観測できるため、span 無しでも完全な暗闇にはならない。

### 6.2 stage_attempt を追加しない

`vector.completion.stage_attempt` counter は追加しない。task の落下は `execute/scrape_html_body` span の ERROR で見られ、handled な DB / transport の一時障害は `processing_outcome{result=infra_error}` として可視化されるため。

---

## 7. Test Requirements

curation / assessment / embedding と同じハーネス分担に倣う。helper は `tests/logfire/_metric_helpers.py` を再利用する。

### 7.1 Metric Emit

- 本文取得 + 永続化 + audit commit 後に `processing_outcome{result=succeeded}` が +1 される。
- scrape 失敗 handler が `is_infra_scrape_failure` どおり emit する (`updated == True`)。
  - `retryable=True` の代表 transport (例 `FetchNetworkError` / `FetchOriginServerError` / `FetchRateLimitedError`) → `infra_error`。
  - `retryable=False` の代表 transport (例 `FetchAccessDeniedError` / `FetchRobotsDisallowedError` / `FetchResourceNotFoundError`) → `failed`。
  - content 失敗 4 variant → `failed`。
  - retry (`_handle_temporary` pending) と exhausted (`close_claimed`) のどちらでも、性質に応じた同じ result を emit する。
- complete 棄却 (`CompletionRejection`) → `failed`。
- persist crash の `SQLAlchemyError` → `infra_error`、非 DB 想定外 → `failed`。
- ready-build: `CanonicalArticleUrlInvalid` / `ObservedArticleInvalid` / `SourceNotRegistered` / `EventType.FAILED` の typed error → `failed`、`SQLAlchemyError` → `infra_error`。
- 各 path は割り当て外の result を一切 emit しない (3 値排他を全件検証)。

### 7.2 Non-emitted Cases

- ready-build blocked (`IncompleteArticleMissing` / `IncompleteArticleNotRunning`、`EventType.SKIPPED`) は emit されない。
- `CompletionSuperseded` / `CompletionUrlConflict` は emit されない。
- 失敗 handler の `updated == False` (claim 喪失 / stale attempt) は emit されない。

### 7.3 Attribute Safety

- data point attributes は `{"result": <value>}` のみ。
- metric dump に `incomplete_article_id`, `analyzable_article_id`, `source_id`, URL, HTTP status, reason code, body sample, error message, failure_kind が混入しない。

### 7.4 Emit Independence From Best-effort Audit

best-effort audit の drop が emit を抑止しないことを固定する (best-effort 経路のみ)。

- `handle_persist_crashed` の audit が DB 失敗 (drop) しても `infra_error` / `failed` は emit される。
- ready-build failed の audit (`_append_ready_build_error_audit`) が drop されても `failed` / `infra_error` は emit される。

注: scrape 失敗 / complete 棄却 handler の audit は状態遷移と同一 tx で atomic (best-effort ではない) ため、この invariant の対象外。これらは tx commit と `updated == True` を emit の前提とする (§5.2)。

### 7.5 Claim-loss Gating

- 失敗 handler で `close_claimed` / `schedule_retry` が `updated == False` を返したとき、`processing_outcome` を emit しない (3 値すべて 0)。
- `updated == True` のときのみ分類どおり emit する。
- 失敗 handler の atomic-tx 内 commit が DB 失敗したとき (§5.4)、`processing_outcome` を emit せず例外が task へ貫通する (3 値すべて 0、例外伝播を検証)。

### 7.6 Content Failure Totality

§2.4 の網羅性を固定する。

- `typing.get_args(ScrapeContentFailure)` の全 variant を列挙し、各々が `is_infra_scrape_failure` で `False` (= `failed`) になる。
- 述語は content 側を明示 match + `assert_never` で締めるため、新 variant 追加時は型検査が落ちる。テストは `get_args(ScrapeContentFailure)` のメンバ集合を期待集合に pin し、union 拡張を検知する。
- 代表 `ExternalFetchError` (retryable=True / False 各 1) が `retryable` どおり `True` / `False` を返す (述語が性質 SSoT を読むことの確認)。
