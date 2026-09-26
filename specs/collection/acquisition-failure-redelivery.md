# 取得Consumerの失敗を再配信と受信完了に分ける

Status: Implemented
作成日: 2026-09-26

## Problem

取得Consumerはすべての失敗をSQSの再配信へ返していた。403・404・読取失敗のように再試行しても変わらない失敗も、計5回取得してからDLQへ入っていた。

## Evidence

- [外部取得の失敗判断](./external-fetch-failure-classification.md)と[取得工程の共通HTTPエラーへの移行](./acquisition-common-http-errors.md)で、取得工程が再試行可否を判断できる。
- 前例: [記事補完の失敗判断](../pipeline/article-completion-consumer.md#補完工程の確定した失敗判断)は判断を結果として返し、handlerが応答を決める。

## 判断と契約

`classify_acquisition_failure(exc, *, now)`は`retry`（SQSの再配信に任せる）か`abandon`（受信完了にし、次の定期投入に任せる）を返す。

| 失敗 | 判断 |
|---|---|
| HTTP起因（`HttpResponseError`・`HttpTransportError`・`HostBlockedError`） | 外部取得の失敗判断が再試行可能ならretry、不可ならabandon |
| `UnreadableResponseError` | abandon |
| `RssFeedErrors` | どれか1つのフィードがretryならretry、それ以外はabandon |
| DB障害・その他 | retry |

- Consumerは取得処理の失敗を`AcquisitionFailed(error, decision)`として返し、handlerはretryだけを`batchItemFailures`に入れる。
- ソース解決の失敗（有効だが未登録のソース・DB読み取り失敗）と不正な依頼は、従来どおり送出して再配信する。
- 監査の`failure_action`に判断を記録し、ログの`acquisition_message_processed`に`message_disposition`を加える。

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
