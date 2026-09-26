# 取得工程を共通HTTPエラーへ移し、旧ExternalFetchError群を撤去する

Status: Implemented
作成日: 2026-09-26

## Problem

取得工程の読み取りはHTTPの失敗を旧`ExternalFetchError`群へ変換し、エラー自身が`retryable`を持っていた。取得処理はそれを`AcquisitionReadError`（`RETRYABILITY`・`FAILURE_KIND`を持つmarker）に包み、監査はmarkerから再試行可否を読んでいた。このため[外部取得の失敗判断](./external-fetch-failure-classification.md)を取得工程から使えず、取得Consumerの再配信の失敗分類（後続）の前提が揃っていない。

## Evidence

- 取得のHTTP呼び出しは`rss_reader`・`crossref_reader`・`algolia_hn_reader`・`RawHttpClient`（sitemap・HTML listing）の4か所で、いずれも旧変換`external_fetch_error_from_exception`を通っていた。
- [共通HTTPの責任](../../backend/app/http/README.md#エラーの共通契約と変換責任)と[記事補完の仕様](../pipeline/article-completion-consumer.md#エラーと工程の責務): エラーは事実だけを持ち、判断は工程が持つ。工程名を付けるためだけに共通HTTPエラーを包み直さない。
- 前例: 補完の取得境界`_open_response`と、判断の`code`・`http_status`・`reason_code`を記録する補完の監査。
- 監査payloadは`extra="ignore"`のため、列を削っても保存済みの行を読める。
- 旧エラー群を使っていたのは取得工程と取得の監査だけだった。

## HTTP呼び出しの契約

`get_source_response(client, url, *, params=None)`（`article_acquisition/tools/source_http.py`）が4か所のGETを担う。

- 成功応答を返す。応答受信直後・status確認前にUTCの受信時刻を記録し、非成功応答は`HttpResponseError`（status・受信時刻・生のRetry-After）にする。
- 通信失敗は`HttpTransportError`（段階・理由）にし、元の例外を原因に残す。
- 宛先拒否`HostBlockedError`と、通信失敗と確認できない例外は元のまま伝える。
- clientの生成・ヘッダー・timeout・本文の読み方は各readerが持つ。

## 取得のエラー

- `AcquisitionError`・`AcquisitionReadError`・`map_origin_to_acquisition`を撤去し、取得・読取の失敗は発生した例外のまま伝える。
- `RssFeedErrors`はフィードごとの失敗（`HttpResponseError`・`HttpTransportError`・`HostBlockedError`・`UnreadableResponseError`）を保持するだけとし、再試行可否を持たない。
- 読取失敗`UnreadableResponseError`は取得工程のエラーとして維持する。

## 監査の記録

| 例外 | outcome_code | retryability | failure_kind | payload |
|---|---|---|---|---|
| `HttpResponseError`・`HttpTransportError`・`HostBlockedError` | 外部取得の失敗判断のcode | 判断が再試行可能ならretryable、不可ならnon_retryable | `external_fetch` | `http_status`（応答）、`reason_code`（通信失敗の理由）。`error_message`なし |
| `UnreadableResponseError` | `reason.value`（`read_*`） | non_retryable | `unreadable_response` | `read_format`・`read_field`・`read_parser_position`、PII-freeの既定メッセージ |
| `RssFeedErrors` | `rss_feed_errors` | どれか1つのフィードが再試行可能ならretryable | `rss_feeds` | `feed_failures`の各要素に`code`・`http_status`・`reason_code` |
| DB障害 | 従来どおり | 従来どおり | 従来どおり | 従来どおり |
| その他 | `unexpected_error` | unknown | `unknown` | 従来どおり |

- 再試行可否は外部取得の失敗判断から導き、判断表を取得側で再定義しない。
- 共通HTTPエラーと宛先拒否は自由文を載せず、生のRetry-Afterも記録しない。
- `fetch_reason`・`fetch_retry_after_seconds`を撤去し、通信失敗の理由は補完と同じ`reason_code`列に残す。

## フィード失敗のログ

`source_feed_fetch_failed`は`source`・`feed`・`code`・`http_status`・`reason_code`を出す。`code`はHTTP起因なら外部取得の失敗判断のcode、読取失敗は`reason.value`とする。一部のフィードだけが失敗しソースが成功した場合、このログがフィードの失敗の唯一の記録になる。

## 振る舞いの変化

- outcome_code: 独自の読み取りを持つソースでは`fetch_*`から`http_response_error`・`http_transport_error`・`host_blocked`になる。RSS宣言のソースは`rss_feed_errors`のままで、フィード単位の`code`が同様に変わる。管理画面のソース健全性に出る失敗理由は、補完と同じ粒度になる。
- CONNECT 403: 旧`FetchEgressBlockedError`（再試行不可）から、通信失敗として再試行可能になる。
- 分類できない例外（`httpx.DecodingError`等）: 旧`FetchNetworkError`（再試行可能）への変換をやめ、監査は`unexpected_error`になる。複数フィードのRSSでは、そのフィードで続行せずソース全体の失敗になる。
- 再配信の挙動は変わらない（取得Consumerは引き続きすべての失敗をSQSへ返す）。

## Invariants

1. 再配信の挙動と、取得成功時の保存・監査・Outboxを変えない。
2. エラーは事実だけを持ち、`retryable`・`RETRYABILITY`・`FAILURE_KIND`を持たない。共通HTTPエラーを取得工程のエラーで包み直さない。
3. 監査の再試行可否は外部取得の失敗判断から導く。
4. `failure_kind`の値（`external_fetch`・`unreadable_response`・`rss_feeds`）を維持する。
5. 分類できない例外を通信障害へ丸めない。
6. 監査・ログに本文・生のRetry-After・例外の自由文を新たに出さない。

## Non-goals

- 取得の失敗判断とhandlerの変更、[取得依頼の定期投入](../pipeline/source-dispatch-scheduler.md)§4の変更（後続）。
- IAM・インフラ・DB schema・`failure_action`の記録。
- `RawHttpClient.fetch`とsitemap・HTML listing readerの`source_name`引数の整理。今回の変更で使われなくなるが、ソース定義まで波及するため別途扱う。
- 過去の仕様・plansの書き換え。直接置き換わる2本に注記するだけとする。

## Done

- 旧`external_fetch_errors.py`・`external_fetch_error_mapping.py`とmarkerが消え、readerは共通HTTPエラーを上げる。
- 監査とログが上記の形で記録される。
- 単体・DB統合・ローカルの取得テストが成功する。

## 実装記録（2026-09-26）

- 追加: [取得先へのGET](../../backend/app/collection/article_acquisition/tools/source_http.py)。撤去: 旧エラー群・旧変換・markerと、その専用テスト4本。
- テスト: HTTP呼び出しの契約は[GETのテスト](../../backend/tests/collection/article_acquisition/tools/test_source_http.py)、監査の記録は[失敗記録のテスト](../../backend/tests/collection/test_source_acquisition_failure_recording.py)（実DB）が正本。readerとソースのテストは、共通の応答エラーが伝わる代表ケースに絞った。
- 検証: app全体と変更ファイルのRuff lint・format、単体7,009件、DB統合67件（`tests/collection/`・`tests/audit/`）、`local_tests/acquisition`26件（実DB、失敗時の再配信を含む）が成功した。
