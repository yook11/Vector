# SQS publisher の失敗契約と共通分類への接続

Status: Implemented

## Problem

SQS publisher は現在、SDK例外をそのまま伝播している。
呼び出し元がbotocoreやAWSのエラーコードに依存せず、送信失敗の原因を判断できる契約を作る。
分類できない例外もpublisherの操作境界で受け止め、後から想定外の失敗と判別できるようにする。

## Evidence

- `backend/app/http/failure.py`: `HttpTransportFailure`（`HttpTransportStage` / `HttpTransportFailureReason`）と通信例外の分類関数は実装済み。
- `backend/app/outbox/publish_errors.py`: 通信・サービス応答・設定・イベント不正の4種類は定義済みで、サービス理由と想定外例外は未実装。
- `backend/app/outbox/sqs_publisher.py`: ベクトル生成向けSendMessageのみ実装済みで、例外変換とSDK再試行設定は未実装。
- `backend/app/outbox/publisher.py`: `EventPublisher` は成功時None、失敗時例外という契約のみ。
- `backend/app/lambda_handlers/outbox_relay.py`: DB接続確認のみで、relayループは未実装。
- `specs/pipeline/outbox-sqs-message-contract.md`: 同一イベントの再送でID・内容を維持し、重複を許容する。
- 調査・検証対象SDK: botocore 1.43.59。

一次資料:

- [SQS SendMessage](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessage.html)
- [SQS Common Errors](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/CommonErrors.html)
- [SQSの一時資格情報](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-using-temporary-security-credentials.html)
- [botocoreの資格情報取得](https://github.com/boto/botocore/blob/develop/botocore/credentials.py)
- [SDKの再試行設定](https://docs.aws.amazon.com/boto3/latest/guide/retries.html)

## Invariants

1. AWS例外型・AWSコードとの対応表はSQS adapter側が所有し、publisher共通定義とrelayはAWS SDKをimportしない。
2. 共通の理由は原因を表し、retryable・待ち時間・停止方針は持たない。
3. SDKのSQS送信は`total_max_attempts=1`とし、内部再試行を行わない。
4. 到達可能性は単一の対象サービスへの通信試行についての情報で、成功・処理完了・イベント全体の未配信を保証しない。
5. 資格情報取得先への通信失敗を、SQSへの通信失敗やSQSのエラー応答と誤認しない。
6. 正常終了はSQSへの送信成功を表し、consumerの処理完了は表さない。
7. 同じOutboxイベントの再送で、event_id・時刻・payloadを生成し直さない。
8. 想定外の通常例外も`PublishUnexpectedError`として伝えるが、キャンセルやプロセス終了を表す`BaseException`を包括しない。
9. 例外文面やログにSDKの自由文・認証情報・Queue URL・イベント本文を無条件に含めない。

## Non-goals

- relayループ、バックオフ、試行上限、配信停止・保留方針、DB更新の実装。
- HTTP応答失敗・リクエスト失敗の共通型の追加。
- fetch / Gemini / Tavilyなど既存adapterの移行。
- 他工程への送信先展開、SendMessageBatch、consumerの重複排除。
- AWSリソース、認証方式、権限、DB schema、依存パッケージの変更。
- あらゆるAWSサービスのエラー一覧の共通化。

## 失敗の契約

| 例外 | 情報 | 境界 |
|---|---|---|
| `PublishTransportError` | `HttpTransportFailure` | 対象サービスとの通信失敗 |
| `PublishServiceError` | 共通reason、サービスコード、HTTP status、取得できる場合のrequest ID | 対象サービスからのエラー応答 |
| `PublishConfigurationError` | 設定・資格情報のreason | 送信の前提条件を満たせない |
| `PublishEventInvalidError` | イベント不正のreason | イベントから送信内容を構築できない |
| `PublishUnexpectedError` | `unexpected_exception`、元の例外型、処理段階 | 上記に分類できない通常例外 |

共通reasonが通常の処理分岐の根拠であり、元のサービスコードは調査用に保持する。
元の例外文面をreasonとして使わない。
既存の`PublishError`の「分類できた失敗のみ」という説明を、想定外の失敗も含む契約へ更新する。

### サービス応答の共通reason

`PublishServiceReason`を定義し、`PublishServiceError`に必須のreasonを追加する。

| reason | AWSコードの対応 |
|---|---|
| `throttled` | `RequestThrottled`, `KmsThrottled`, `ThrottlingException` |
| `authentication_failed` | `InvalidClientTokenId`, `MissingAuthenticationToken` |
| `access_denied` | `AccessDenied`, `AccessDeniedException`, `NotAuthorized`, `KmsAccessDenied`, `OptInRequired` |
| `destination_not_found` | `QueueDoesNotExist` |
| `request_rejected` | `InvalidAddress`, `InvalidMessageContents`, `UnsupportedOperation`, `InvalidAction`, `InvalidParameterCombination`, `InvalidParameterValue`, `InvalidQueryParameter`, `MalformedQueryString`, `MissingAction`, `MissingParameter`, `ValidationError` |
| `security_rejected` | `InvalidSecurity`, `IncompleteSignature` |
| `request_expired` | `RequestExpired` |
| `encryption_error` | `KmsDisabled`, `KmsInvalidState`, `KmsNotFound`, `KmsOptInRequired`, `KmsInvalidKeyUsage` |
| `service_unavailable` | `InternalFailure`, `ServiceUnavailable`、既知コードに該当しない5xx応答 |
| `unclassified` | 上記に分類できない対象サービスのエラー応答 |

- 既知のAWSコードをHTTP statusより優先する。
- 403を一律access_denied、404を一律destination_not_found、400を一律request_rejectedにしない。
- `KmsNotFound`は送信先のキュー不在ではないため、destination_not_foundにしない。
- `KmsDisabled`は公式説明と名前から原因を一意に確定しにくいため、暗号化関連と元コードを保持し、再試行可否を推測しない。
- 対応表にない期限切れ資格情報コード・旧コード別名はunclassifiedとして保持する。
- 期限切れを含む詳細は元のサービスコードで調査できるようにし、relayにAWSコードの判定を持ち込まない。

### 設定・資格情報とイベント不正

- 既存の`missing_credentials` / `incomplete_credentials` / `missing_region`を維持する。
- `credentials_retrieval_failed`を追加し、資格情報の未設定と取得・更新失敗を区別する。
- `NoRegionError`などクライアント生成時の例外は生成境界で扱い、publishだけで捕捉できると仮定しない。
- `ClientError.operation_name`が`SendMessage`以外の場合、SQS応答の対応表を適用しない。
- STSなどの失敗は資格情報取得経路と確認できる場合だけ取得失敗に分類し、確認できないものは想定外として保持する。
- イベント不正は既存の未対応event_type・timezoneなし日時・シリアライズ失敗を対象とする。
- `ValueError`や`TypeError`をpublish全体でイベント不正に変換せず、既知の検証・シリアライズ処理の範囲でのみ変換する。
- SDKの`ParamValidationError`をイベント不正と自動判断しない。adapterが不正な要求を構築した可能性を残す。

### 想定外の失敗と記録

- 分類関数は分類不能ならNoneを返し、publisherが最終的に`PublishUnexpectedError`へ変換する。
- 処理段階は`initialize` / `prepare_event` / `resolve_credentials` / `send` / `classify_failure` / `cleanup`を区別する。
- 既に`PublishError`である例外は二重に包まない。
- 分類処理自体の失敗で元の障害を見失わないよう、元の例外型と分類処理失敗を安全な診断情報で区別する。
- `reason`と元の例外型・処理段階を保持し、後のrelay側でevent_idと関連付けられる契約にする。
- 元例外の追跡は既存のLogfire例外処理と照合し、例外チェーン・traceback経由で自由文が外部に漏れないことを検証する。
- 新たな通知基盤や永続化先は作らず、実際の通知・記録処理はrelay実装へ残す。

## 実装順序

### Step 1: publisher共通定義を完成させる

対象:

- `backend/app/outbox/publish_errors.py`
- `backend/tests/outbox/test_publish_errors.py`

作業:

- `PublishServiceReason`とサービス例外のreason・request IDを追加する。
- 資格情報取得失敗のreasonと`PublishUnexpectedError`の診断情報を定義する。
- 共通定義にbotocoreへの依存を追加しない。

完了条件: 失敗の種類と調査情報を保持でき、外部自由文を例外文面へ露出しない。

### Step 2: AWS依存の分類関数を実装する

対象:

- `backend/app/outbox/sqs_error_mapping.py`（新規）
- `backend/tests/outbox/test_sqs_error_mapping.py`（新規）

作業:

- `publish_error_from_sqs_exception(exc) -> PublishError | None`を実装する。
- botocoreの資格情報例外、SendMessageのClientError、共通の通信分類を適切な順で処理する。
- 対応表はこのモジュールが所有し、共通型にAWS固有の定数を置かない。
- SDKサービスモデルにあるSendMessageの13種類と共通エラーをテスト入力として列挙する。
- 未知コード、metadata欠損、別操作のClientError、非SDK例外も検証する。

完了条件: 既知の失敗を共通理由へ変換でき、対象外の原因を誤分類しない。

### Step 3: SQSクライアントの単一試行契約を確立する

対象:

- `backend/app/outbox/sqs_client.py`（新規）
- `backend/app/outbox/sqs_publisher.py`
- 対応するunit test

作業:

- 明示的なregionを受け取るクライアント生成口を用意し、botocoreの`Config`で`total_max_attempts=1`を設定する。
- regionやQueue URLは既存の設定層から渡す設計を維持し、環境変数を独自に読み込まない。
- 既存クライアント注入から生成口の注入へ変更し、各publishで資格情報を固定したクライアントを作成・終了する。
- 本番生成口はSqsEventPublisher.from_sessionとし、テスト用factoryも固定資格情報・単一試行の契約を満たす。
- 生成時の資格情報・設定失敗と想定外の失敗も、初期化境界でpublisherの例外に変換する。
- 送信時の資格情報取得・更新が追加通信を行う点と、SQS送信の試行回数を混同しない。
- SDK内部再試行を無効にしても、過去のpublish試行やStandard SQSの重複は排除できないことを契約に残す。

完了条件: transportをmockしたSDK呼び出しで、失敗時もSQS送信が1回で終了することを確認できる。

### Step 4: publisherの操作境界へ配線する

対象:

- `backend/app/outbox/sqs_publisher.py`
- `backend/app/outbox/publisher.py`
- `backend/tests/outbox/test_sqs_publisher.py`

作業:

- 既存の入力検証を`PublishEventInvalidError`へ統一し、`UnsupportedEventTypeError`の既存参照を移行する。
- `send_message`の失敗を分類関数へ渡し、分類不能の場合は`PublishUnexpectedError`に変換する。
- Protocolの成功・失敗契約を明記する。
- 現在のClientError伝播テストを、新しいpublisher例外と保持情報のテストへ更新する。
- 既存のJSON形式、UTC変換、送信先、再送内容維持のテストは残す。

完了条件: 呼び出し元がAWS SDKを知らずに失敗を受け取れ、想定外の通常例外も失敗として明示される。

### Step 5: 契約の同期と最終検証

- `specs/pipeline/outbox-sqs-message-contract.md`の「SDK例外をそのまま伝播」と失敗分類の対象外記述を、実装した範囲に合わせて更新する。
- `/check`に従いlint・format・単体テスト・`make test-integration`を実行する。
- 単体とintegrationを分けて実行し、通常のテストコマンドが未起動DBに接続する既知の問題を避ける。
- 例外文面だけでなく、SDK由来の例外チェーンも安全性の確認対象とする。
- relayへの接続、DBの送信済み更新、停止・通知は未実装と明記する。

## Done

- AWSに依存しないpublisherの失敗契約と、AWS依存の分類実装が分離されている。
- 調査したSendMessage固有エラーと共通エラーに対応があり、未知・対象外・想定外の扱いもテストされている。
- 資格情報取得失敗をSQS送信失敗と取り違えない。
- SDK内部再試行を行わず、既存のイベント送信内容を維持する。
- 想定外の失敗にも安定したreasonと安全な診断情報がある。
- 必要な検証が通り、仕様に実装範囲と残作業が反映されている。

## 検証結果

- ruff lint・format: 成功。
- 全単体テスト: 5,436件成功（追加後の対象テスト105件も成功）。
- DB integration: 1,196件成功、22件skip。
- relayの接続・DB更新・通知は今回の対象外。
