# 取得Consumerの失敗を再配信と受信完了に分ける

Status: Implemented
作成日: 2026-09-26

## Problem

取得Consumerはすべての失敗をSQSの再配信へ返していた。403・404・読取失敗のように再試行しても変わらない失敗も、計5回取得してからDLQへ入っていた。

## Evidence

- [外部取得の失敗判断](./external-fetch-failure-classification.md)と[取得工程の共通HTTPエラーへの移行](./acquisition-common-http-errors.md)で、取得工程が再試行可否を判断できる。
- 前例: [記事補完の失敗判断](../pipeline/article-completion-consumer.md#補完工程の確定した失敗判断)は判断を結果として返し、handlerが応答を決める。

## 判断と契約

`ArticleAcquisitionConsumer.consume()`の戻り値は、`consumer_result.py`に置く不変の`AcquisitionSucceeded | RetryAcquisition | NoRetryAcquisition`の3型とする。

- `AcquisitionSucceeded`は新しく保存した記事数を`created_count`に保持する。
- `RetryAcquisition`は再配信に任せる元例外を`error`に保持する。
- `NoRetryAcquisition`は、対象なし・無効ソースなら`cause`に`missing`・`inactive`を、再配信しない失敗なら元例外を保持する。取得不要は失敗監査に記録しない。`AcquisitionNotRequired`はソース解決内部の結果として残し、Consumerの外へは返さない。

`classify_acquisition_failure(exc, *, now)`は、元例外を持つ`RetryAcquisition`（SQSの再配信に任せる）か`NoRetryAcquisition`（受信完了にし、次の定期投入に任せる）を返す。再配信するかどうかは型で区別し、判断のフィールドは持たない。分類関数が返す`NoRetryAcquisition.cause`は常に元例外とする。

| 失敗 | 判断 |
|---|---|
| HTTP起因（`HttpResponseError`・`HttpTransportError`・`HostBlockedError`） | 外部取得の失敗判断が再試行可能なら`RetryAcquisition`、不可なら`NoRetryAcquisition` |
| `UnreadableResponseError` | `NoRetryAcquisition` |
| `RssFeedErrors` | どれか1つのフィードが`RetryAcquisition`なら`RetryAcquisition`、それ以外は`NoRetryAcquisition` |
| DB障害・その他 | `RetryAcquisition` |

- Consumerは取得処理の失敗の判断結果をそのまま返し、handlerは`RetryAcquisition`だけを`batchItemFailures`に入れる。
- ソース解決の失敗（有効だが未登録のソース・DB読み取り失敗）と不正な依頼は、従来どおり送出して再配信する。
- handlerは既存の`EventReader(acquisition_request_from_message)`で入力を読み、読取失敗・Consumer呼び出しの例外・Consumerから受け取った結果を分けて扱う。Consumerから結果を受け取れなかった場合は`RetryAcquisition`を生成せず、そのメッセージを再配信対象に追加して後続処理へ進む。
- 配送診断は`AcquisitionMessageRecorder`が担う。読取失敗は`operation=parse_message`または`validate_event`、Consumer呼び出しの例外は`operation=consume`で区別し、元例外の自由文や本文は出力しない。既存の`result`・`code`を維持し、所要時間は`perf_counter()`で取得した開始値から共通の`elapsed_ms_since()`で計算し、`duration_ms`へミリ秒で記録する。診断障害で配送判断を変えない。
- 監査の`failure_action`に`retry`・`no_retry`を記録し、ログの`acquisition_message_processed`に`message_disposition`を加える。

## Invariants

1. HTTP起因の再試行可否は外部取得の失敗判断に従う。
2. 取得成功時の処理と、監査のoutcome_code・retryabilityを変えない。
3. SQSの設定・IAM・インフラを変えない。

## Non-goals

- Retry-Afterによる待機。取得はcadenceごとに新しい依頼が届くため、1件を待たせても相手への要求は減らない。
- 常に失敗するソースへの対処、DLQのアラーム。

## Done

- 再試行不可の失敗が1回で受信完了し、単体・DB統合・`local_tests/acquisition`が成功する。

## 実装記録（2026-09-26）

単体7,023件、DB統合67件（`tests/collection/`・`tests/audit/`）、`local_tests/acquisition`26件、変更ファイルのRuff lint・formatが成功した。
