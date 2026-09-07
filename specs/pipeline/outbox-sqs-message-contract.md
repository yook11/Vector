# OutboxからSQSへの送信契約

Status: Partially implemented

## Problem

保存済みOutboxイベントを工程別のSQSへ送るため、メッセージ形式と送信先を固定する。最初にベクトル生成向けを実装し、残りの工程へ適用して1つのPRにまとめる。

## Evidence

- イベント本体: `backend/app/models/outbox_event.py`
- 既存payload定義: `backend/app/collection/{article_acquisition,article_completion}/events.py`、`backend/app/analysis/{curation,assessment}/events.py`
- キューと設定: `infra/aws/outbox_relay.tf`、`backend/app/lambda_handlers/settings.py`

## Invariants

### メッセージ形式

MessageBodyは次の5項目を持つJSONオブジェクトとする。

```json
{
  "event_id": "b8969c8e-5c43-4b5e-9867-b20768551666",
  "event_type": "article.assessed_in_scope",
  "schema_version": 1,
  "occurred_at": "2026-09-07T03:00:00Z",
  "payload": {
    "curation_id": 123,
    "analyzed_article_id": 456
  }
}
```

- 5項目は保存済みOutbox行から取得し、送信時にID・時刻・バージョンを生成し直さない。
- `event_id`はUUID文字列、`schema_version`は整数、`payload`はJSONオブジェクトとする。
- `occurred_at`はUTCのISO 8601文字列とし、末尾は`Z`、元の小数秒がある場合は保持する。
- payloadは既存イベント定義の内容を維持し、event_type・schema_versionを重複して入れない。
- 配信管理項目（試行回数・lease・停止理由など）は送らない。
- MessageAttributesは使用しない。

### 送信先

Standardキューを使用し、event_typeから送信先を決定する。Queue URLは設定層から取得し、payloadには含めない。

| event_type | キューの接尾辞 |
|---|---|
| `article.incomplete_recorded` | `article-completion` |
| `article.acquired` | `article-curation` |
| `article.completed_to_analyzable` | `article-curation` |
| `article.curated_signal` | `article-assessment` |
| `article.assessed_in_scope` | `article-embedding` |

実際のキュー名には既存の`name_prefix`を付ける。未登録のevent_typeは送信せず、代替キューへ流さない。停止・再試行の判断は別途定める。

### 再送

同じOutbox行の再送では、同じevent_idとイベント内容を送る。SQSが発行するMessageIdとは区別する。重複配信を許容し、将来のconsumerがevent_idを重複判定に利用できる契約とする。

## Non-goals

publisherの失敗分類は下記の契約に従う。バックオフと試行上限は下記の再試行・停止ポリシーに従う。実行間隔、処理件数、lease期間は別途決定する。consumer実装、既存Taskiq処理の移行、AWSリソース変更、Scheduler有効化は含めない。

## Done

- ベクトル生成向けから実装し、上記5イベント・4キューすべてに適用する。
- 保存済みイベントが指定形式のMessageBodyと正しいQueue URLに変換される。
- 再送でイベント内容が変わらず、未知のevent_typeでは送信しない。
- 全工程分を1つの日本語PRにまとめる。

## Implementation

`backend/app/outbox/publisher.py`にEventEnvelopeと同期EventPublisher Protocol、`sqs_publisher.py`にベクトル生成向けのSQS送信部品を実装した。本番の生成口はSqsEventPublisher.from_sessionで、SDK session・region・Queue URLを呼び出し元から渡す。成功時はNone、失敗時はPublishErrorを伝える。対応外イベントとタイムゾーンのない日時は送信前に拒否する。

他工程への展開、relayループへの接続、実行検証は未完了。Lambda handlerは接続確認のままとし、relayループの運用条件は別途仕様化する。

## Verification

`backend/tests/outbox/test_sqs_publisher.py`にStubberを使用したunit testを追加した。MessageBodyと送信先、再送時の内容維持、UTC変換と小数秒、対応外イベント・タイムゾーンなし日時の非送信、SDK例外の共通分類への変換を対象とする。既存payload定義を固定するだけのテストは追加していない。

スライス②完了時点でlint・format、全単体テスト5,572件、integration test 1,217件（22件skip）が成功した。AWS上での送信確認・デプロイは行っていない。

## Publisherの失敗契約

- PublishTransportError: HttpTransportFailureで対象サービスへの通信失敗を表す。
- PublishServiceError: reason・service_error_code・status_code・request_idを保持し、処理分岐は共通reasonを使う。
- PublishConfigurationError: missing_credentials・incomplete_credentials・missing_region・credentials_retrieval_failedを区別する。
- PublishEventInvalidError: unsupported_event_type・invalid_occurred_at・serialization_failedを区別する。
- PublishUnexpectedError: reason=unexpected_exception、original_exception_type、phase、任意のclassification_exception_typeを保持する。

サービス理由はthrottled、authentication_failed、access_denied、destination_not_found、request_rejected、security_rejected、request_expired、encryption_error、service_unavailable、unclassifiedとする。
AWSコードとの対応はSQS adapterのsqs_error_mappingが所有する。
既知コードをstatusより優先し、未知コードは5xxだけservice_unavailable、それ以外はunclassifiedとする。
SendMessage以外のClientErrorをSQS応答として分類しない。
共通型は再試行可否や配信停止方針を持たない。

送信前に本文を構築し、SDK providerから資格情報を取得・更新して固定した後、その資格情報でSQSクライアントを作成する。
各publishでクライアントを生成・終了し、SQSのtotal_max_attempts=1を固定する。
テスト用client_factoryも、確定済み資格情報・単一試行・毎回独立したクライアントという同じ契約を満たす必要がある。
資格情報取得先への通信失敗はcredentials_retrieval_failedであり、SQSのtransport失敗ではない。
HttpTransportFailureの到達可能性はその単一通信試行の情報で、過去の送信やconsumerの処理完了を保証しない。

通常例外は操作境界で受け止め、元例外はcauseに保持するが、SDK自由文・本文・資格情報・送信先URLを例外文面へ出さない。
phaseはinitialize、prepare_event、resolve_credentials、send、classify_failure、cleanupを区別する。
分類処理失敗時は元例外と分類失敗の型を保持し、終了処理の失敗は先行する送信失敗を上書きしない。
cleanupの失敗はSQS受付成功後に起きるため、未送信の根拠にしてはならない。
BaseExceptionによるキャンセル・終了は通常失敗へ変換しない。
Logfireの既存export秘匿処理を維持し、実際の記録・通知とevent_idとの関連付けはrelay実装に残す。

単体検証はAWSコード表、未知・不正metadata、資格情報の取得・更新失敗、SDKの単一試行、例外の診断情報、終了処理、Logfire exportの秘匿を対象とする。

## 再試行・停止ポリシー（スライス①）

Problem: publisherが返す原因から、再試行・停止を判断する規則を一箇所に定義する。
Evidence: publish_errorsの共通分類と、claim_ready_batchが確保時にattempt_countを加算する契約に従う。

publish_failure_policy.decide_publish_failure(error, attempt_count, jitter)は、副作用のない判断関数とする。
結果は不変のRetryPublish(delay)またはStopPublish(reason)で返す。
内部の原因判定はRetryableまたはNonRetryable(reason)を返し、再試行対象かどうかを明示する。
NonRetryableはStopPublishへ変換し、Retryableに対して回数上限を適用し、待ち時間付きのRetryPublishまたは上限到達のStopPublishに確定する。
再試行しない理由はNonRetryableReasonのnon_retryable_failure、retry_exhausted、unclassified_failure、unexpected_failureとし、元の失敗情報はPublishErrorに保持する。

| 失敗 | 方針 |
|---|---|
| transportのDNS・connect・各timeout・network_io・remote_protocol・unknown | 上限付き再試行 |
| TLS | non_retryable_failureで停止 |
| proxyのstatusなし・429・500〜599 | 上限付き再試行 |
| proxyのその他のstatus | non_retryable_failureで停止 |
| serviceのthrottled・service_unavailable | 上限付き再試行 |
| serviceのunclassified | unclassified_failureで停止 |
| serviceのその他のreason（request_expiredを含む） | non_retryable_failureで停止 |
| configuration・event invalidの全reason | non_retryable_failureで停止 |
| unexpected・未対応のPublishError | unexpected_failureで停止 |

再試行対象は初回を含め最大5回とし、1〜4回目の失敗後の基準待ち時間を30秒・2分・10分・30分とする。
実際の待ち時間は基準時間 × (0.8 + 0.4 × jitter)で、jitterは呼び出し元が渡す0〜1の有限値とする。
5回目以降はretry_exhaustedで停止するが、即停止対象の理由は上書きしない。
attempt_countは確保後の1以上の整数で、実送信回数ではなく配信試行の確保回数を意味する。
非整数の回数・非数値のjitter（boolを含む）はTypeError、値の範囲違反はValueErrorとする。
停止対象でもこれらの入力条件を満たす必要がある。

Invariants: 時刻取得・乱数生成・AWSコードの再解釈・DB更新・通知を行わず、同じ入力に同じ結果を返す。
到達可能性がTrueでも重複許容の再試行方針を変えず、入力の診断情報を変更しない。
cleanup段階のPublishUnexpectedErrorはValueError、非PublishErrorはTypeErrorで拒否し、relay側で別経路として扱う。

Non-goals: failure handlerによる状態更新、Slack通知、relay接続、停止後の再開は後続スライスとする。
Done: 全分類、proxy境界、試行上限、jitter境界、不正入力、判断の不変性を単体テストで保証する。
この段階では稼働中の配信処理の再試行動作は変更しない。

## 配信失敗の状態更新（スライス②）

Problem: policyの判断をDBへ確定し、判断のみの状態と保存成功を区別する。
Evidence: OutboxDeliveryRepositoryは更新成否をboolで返し、トランザクションの確定は呼び出し元が担う。

PublishFailureHandler.handle(event=ClaimedOutboxEvent, error=PublishError)は、イベント単位の短いトランザクションを管理する。
session factoryはcaller_managed_session_factoryと互換のsession開閉専用factoryを注入する。
jitter生成関数は既定でrandom.randomを使い、テストでは固定値を注入する。

1. jitterを1回生成し、確保後のattempt_countと失敗をpolicyへ渡す。
2. 判断が得られてからsessionを開く。
3. RetryPublishはschedule_retry、StopPublishはstop_deliveryで条件付きUPDATEを行う。
4. 更新成功時はcommit、更新なしはrollbackする。
5. session終了後に、RetryScheduled(delay)、DeliveryStopped(reason)、DeliveryUpdateSkippedのいずれかの不変な結果を返す。

delivery_stop_reasonにはNonRetryableReason.valueのみ保存し、失敗の自由文や本文を含めない。
元の原因・診断情報は入力のPublishErrorに残し、Slackへの通知と失敗記録は後続スライスでevent_idと関連付ける。
更新なしはtoken不一致・lease失効・処理済み・イベント不在などを含み、理由を推測しない。
DB時刻と更新権限はrepositoryへ委譲し、事前SELECTやhandler側での時刻比較を追加しない。

Invariants: 本文・試行回数・published_atを変更せず、lease解除は既存の更新処理に任せる。
policyの拒否やjitter生成失敗はsessionを開かず伝播する。
DB更新・commit・session終了が失敗した場合は成功結果を返さず、DB例外は既存session factoryの変換に従い、PublishErrorへ再分類しない。
未確定の変更はsession終了時に取り消すが、commit後の終了失敗で確定済み変更を取り消したとは扱わない。
キャンセルやプロセス終了は通常の配信失敗として捕捉しない。

Non-goals: Slack通知、relay接続、成功処理、停止後の再開、schemaや既存policy・repositoryの変更は含めない。
Done: 別sessionから予約・停止の確定を確認し、更新不能・再実行・更新障害・commit障害・終了障害の結果を実DBテストで保証する。
