# 外部取得の失敗判断を補完工程から collection 共通へ切り出す

Status: Implemented
作成日: 2026-09-26

## Problem

外部からニュースを取得するときのHTTP失敗の意味づけ（時間を置けば結果が変わりうるか、相手の待機指示、調査対象か）が補完工程の分類関数の中にあり、取得工程から使えない。取得と補完はどちらも外部からニュースを取得する工程で、同じHTTPの事実に対する意味づけは工程によって変わらない。

取得Consumerは現在すべての失敗をSQSの再配信へ返している（[取得依頼の定期投入](../pipeline/source-dispatch-scheduler.md)§4）。取得側の再配信の失敗分類は、この判断を使う前提で後続に実装する。

## Evidence

- [補完の失敗分類](../../backend/app/collection/article_completion/consumer_failure_classification.py): HTTP応答・通信失敗・宛先拒否の判断とRetry-After解釈を、補完工程の判断として持っていた。
- [共通HTTPの責任](../../backend/app/http/README.md#エラーの共通契約と変換責任): 共通HTTPは発生事実だけを保持し、終了・再試行・待機の判断は利用機能が持つ。
- [記事補完の仕様](../pipeline/article-completion-consumer.md#エラーと工程の責務): エラーは`retryable`を持たず、判断は工程のハンドラーが持つ。
- [HTTP status変換のcollection直下への集約](./article-collection-external-fetch-error-mapping.md): 取得と補完が共有する概念を、工程間の依存を作らないようcollection直下に置いた前例。
- 取得工程の読み取りは、旧`ExternalFetchError`群とエラー自身の`retryable`を使っている。共通HTTPエラーへの移行は後続で行う。

## 責務の分担

| 担当 | 責任 | 持たないもの |
|---|---|---|
| 共通HTTP（`app/http`） | 応答status・受信時刻・生のRetry-After、通信失敗の段階と理由、宛先拒否を事実として伝える | 再試行・終了・待機の判断 |
| 外部取得の失敗判断（本仕様） | 失敗が時間を置けば変わりうるか、相手の待機指示が有効か、調査対象か | 工程の後始末、HTTP以外の失敗の判断 |
| 各工程（補完・取得） | 判断を後始末（再配信・closed化・受信完了）へ写し、工程固有の失敗を判断する | HTTPの事実の再解釈 |

## 判断の契約

`classify_external_fetch_failure(exc, *, now)`は純粋関数であり、DB・SQS・ログ・現在時刻の取得を行わない。

- 戻り値は`RetryableFetchFailure | NonRetryableFetchFailure | None`とする。
  - `RetryableFetchFailure`: 時間を置けば結果が変わりうる。`code`・`retry_at`（`RetryAt | None`）・`requires_investigation`を持つ。
  - `NonRetryableFetchFailure`: 今の要求では取得できず、再試行しても結果が変わらない。`code`を持つ。
  - `None`: HTTP起因の失敗ではなく、工程が判断する。
- `code`は`HttpResponseError`・`HttpTransportError`の既存CODE（`http_response_error`・`http_transport_error`）とし、`HostBlockedError`は`host_blocked`とする。
- 調査対象は、取得先の拒否ではなく環境側の問題か、分類が確かでないことを示す。即時通知の指示ではなく、出力や緊急度は工程と後続処理が判断する。
- 元例外・原因チェーン・生のRetry-Afterを変更しない。

| 原因 | 判断 | 調査対象・補足 |
|---|---|---|
| 408 / 421 / 429 | 再試行可能 | 421は別接続での再試行、429は有効な待機指示を採用 |
| 425 | 再試行可能 | 調査対象。TLS Early Dataに関する拒否であり、Early Dataによる再送は追加しない |
| 407 / 511 | 再試行可能 | 調査対象。プロキシ・ネットワークの認証問題で、取得先の拒否ではない |
| 500 / 502 / 503 / 504、501・505以外のその他5xx | 再試行可能 | サーバ・中継経路の失敗 |
| 501 / 505 | 再試行不可 | 現在の機能・HTTPバージョンでは取得不可 |
| 401 / 403 / 404 / 410、上記以外の4xx | 再試行不可 | 現在の要求では取得不可。403だけからボット拒否と推測しない |
| 3xx | 再試行不可 | リダイレクト非追従方針を維持 |
| HttpResponseErrorに入った1xx・2xx・範囲外 | 再試行可能 | 調査対象。成功への読み替えは行わない |
| HttpTransportError | 再試行可能 | 通信の段階または理由がUNKNOWN、proxy_statusが407・511なら調査対象 |
| HostBlockedError | 再試行不可 | 明示的な宛先保護による拒否 |

- `proxy_status`は通信失敗の事実であり、取得先のHTTP応答の判断へ流用しない。プロキシ接続時の403だけで再試行不可にしない。
- HTTPの根拠: [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-15)、[RFC 6585](https://www.rfc-editor.org/rfc/rfc6585.html)、[RFC 8470の425](https://www.rfc-editor.org/rfc/rfc8470.html#section-5.2)。

### Retry-Afterの解釈

- 再試行可能と判断したHttpResponseErrorだけに適用し、前後空白を除いたASCII非負整数は`received_at + 秒数`、HTTP日時は絶対日時として解釈する。
- 標準ライブラリ`email.utils.parsedate_to_datetime`を利用し、旧HTTP日時形式も受け入れる。タイムゾーンのないasctime形式はUTCとして扱い、結果はUTCへ正規化する。[Python日時解析](https://docs.python.org/3.13/library/email.utils.html#email.utils.parsedate_to_datetime)
- `now`と`received_at`は呼び出し側が渡すタイムゾーン付き日時とし、関数内で現在時刻を取得しない。候補時刻がnow以前、0秒、欠如・空値・不正値・日時として表現範囲外の値は`retry_at=None`とする。
- Noneは追加待機の指示なしであり、工程の通常の再試行間隔を変える意味ではない。再試行不可の失敗を、ヘッダーの有無で再試行可能に変更しない。
- 有効な未来日時は短縮しない。待機の上限や実現方法は各工程が決める。[RFC 9110 Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)

`RetryAt`はタイムゾーン付き日時をUTCへ正規化して不変に保持し、`remaining(now)`は期限経過後に0を返す（元の日時は変えない）。

## Invariants

1. 補完工程の判断結果（再試行・終了、`code`、`retry_at`、`requires_investigation`）と監査・ログ出力を、切り出しの前後で変えない。
2. 共通HTTPに判断を持ち込まない。
3. 本判断は`article_completion`・`article_acquisition`をimportしない。
4. 判断表の正本テストは本判断のテストに置き、工程側で再定義・重複検証しない。

## Non-goals

- 取得工程の変更（共通HTTPエラーへの移行、再配信の失敗分類）。
- 旧`ExternalFetchError`群の撤去。
- robots・応答サイズ上限・Content-Type・取得期限の共通化。現在は補完だけが発生させるため、補完工程に残す。
- 判断表と`code`の粒度の変更。

## Done

- 判断関数と`RetryAt`がcollection直下にあり、補完工程のHTTP起因の3種がこの判断を経由する。
- 判断表とRetry-Afterの正本テストが本判断のテストにあり、補完工程のテストは判断の写し方だけを確認する。
- 補完の仕様・配送仕様・共通HTTPのREADMEが本仕様を参照する。
- 変更範囲のlint・format・単体テストが成功する。

## 後続

1. 取得工程の読み取りと監査を共通HTTPエラーへ移し、旧`ExternalFetchError`群を撤去する。再配信の挙動は変えない。
2. 取得工程の失敗判断とhandlerを実装し、本判断の結果を再配信・受信完了へ写す。[取得依頼の定期投入](../pipeline/source-dispatch-scheduler.md)§4を更新する。

## 実装記録（2026-09-26）

- 追加: [外部取得の失敗判断](../../backend/app/collection/external_fetch_failure.py)。移動: [RetryAt](../../backend/app/collection/retry_at.py)。補完の分類関数は、HTTP起因の判断を補完の判断へ写すだけにした。
- テスト: 判断表・通信失敗・Retry-After・宛先拒否を[判断のテスト](../../backend/tests/collection/test_external_fetch_failure.py)へ移し、判断結果を`code`まで比較する。補完側は写し方の代表5ケースを持つ。
- 検証: 中身を変えていない補完テストを委譲後の実装で実行して172件成功を確認してから、テストを整理した。整理後、`tests/collection/`・`tests/lambda_handlers/`の単体1,380件と、app全体のRuff lint・formatが成功した。`local_tests/completion`はDockerの専用DBとfrontendの依存導入が必要なため未実行。
