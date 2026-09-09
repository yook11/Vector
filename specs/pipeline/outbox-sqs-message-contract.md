# OutboxからSQSへの送信契約

Status: Partially implemented

## Problem

保存済みOutboxイベントをSQSへ送るため、メッセージ形式と送信先を固定する。今回の実装はベクトル生成向けのバッチ送信に限定し、イベントごとの受付成功・送信失敗を呼び出し元へ返す。

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

Standardキューを使用し、event_typeから送信先を決定する。Queue URLは設定層から取得し、payloadには含めない。以下は工程全体の送信先対応であり、実装済みは`article.assessed_in_scope`から`article-embedding`への送信だけである。

| event_type | キューの接尾辞 |
|---|---|
| `article.incomplete_recorded` | `article-completion` |
| `article.acquired` | `article-curation` |
| `article.completed_to_analyzable` | `article-curation` |
| `article.curated_signal` | `article-assessment` |
| `article.assessed_in_scope` | `article-embedding` |

実際のキュー名には既存の`name_prefix`を付ける。イベント種別の許可判定と送信先はPublisherが所有し、現在は`ArticleAssessedInScope.EVENT_TYPE`だけを設定された`embedding_queue_url`へ送る。対応外のevent_typeは本文構築前に個別失敗とし、代替キューへ流さない。停止・再試行の判断は別途定める。

### 再送

同じOutbox行の再送では、同じevent_idとイベント内容を送る。SQSが発行するMessageIdとは区別する。重複配信を許容し、将来のconsumerがevent_idを重複判定に利用できる契約とする。

## Non-goals

publisherの失敗分類は下記の契約に従う。バックオフと試行上限は下記の再試行・停止ポリシーに従う。確保の初期件数は10件、上限到達の停止は100件とする。実行間隔、1起動あたりのバッチ数、lease期間は別途決定する。consumer実装、既存Taskiq処理の移行、AWSリソース変更、Scheduler有効化は含めない。

## Done

- ベクトル生成向け1〜10イベントを最大1回のSQSリクエストで送り、入力順に個別結果を返す。
- 保存済みイベントが指定形式のMessageBodyと正しいQueue URLに変換される。
- 再送でイベント内容が変わらず、未知のevent_typeでは送信しない。
- 部分失敗・不正応答・終了失敗の境界と、件数・サイズ上限をテストで保証する。

## Implementation

`backend/app/outbox/publisher.py`にEventEnvelopeと同期EventPublisher Protocol、`sqs_publisher.py`にベクトル生成向けのSQS送信部品を実装した。本番の生成口はSqsEventPublisher.from_sessionで、SDK session・region・Queue URLを呼び出し元から渡す。公開APIはpublish_batchに統一し、各イベントのPublishSucceededまたはPublishFailed(error=PublishError)をBatchPublishResultに格納する。対応外イベントとタイムゾーンのない日時は個別失敗にし、正常なイベントだけを送信する。

他工程への展開、relayループへの接続、AWS上での実行検証は未完了。Lambda handlerは接続確認のままとし、relayループの運用条件は別途仕様化する。

## Verification

`backend/tests/outbox/test_sqs_publisher.py`にStubberを使用したunit testを追加した。MessageBodyと送信先、再送時の内容維持、UTC変換と小数秒、対応外イベント・タイムゾーンなし日時の非送信、SDK例外の共通分類への変換を対象とする。既存payload定義を固定するだけのテストは追加していない。

スライス②完了時点でlint・format、全単体テスト5,572件、integration test 1,217件（22件skip）が成功した。AWS上での送信確認・デプロイは行っていない。

## Publisherの失敗契約

- PublishTransportError: HttpTransportFailureで対象サービスへの通信失敗を表す。
- PublishServiceError: reason・service_error_code・status_code・request_idを保持し、処理分岐は共通reasonを使う。
- PublishConfigurationError: missing_credentials・incomplete_credentials・missing_region・credentials_retrieval_failedを区別する。
- PublishEventInvalidError: unsupported_event_type・invalid_occurred_at・serialization_failed・message_too_largeを区別する。
- PublishResponseInvalidError: 応答の形式・送信対象との対応の違反を共通のreasonで保持し、受付されなかったとは断定しない。
- PublishUnexpectedError: reason=unexpected_exception、original_exception_type、phase、任意のclassification_exception_typeを保持する。
- PublishIntegrityError: body_checksum_mismatchで送信本文の整合性確認失敗を表し、任意のrequest_idを調査用に保持する。

サービス理由はthrottled、authentication_failed、access_denied、destination_not_found、request_rejected、security_rejected、request_expired、encryption_error、service_unavailable、unclassifiedとする。
AWSコードとの対応はSQS adapterのsqs_error_mappingが所有する。
既知コードをstatusより優先し、未知コードは5xxだけservice_unavailable、それ以外はunclassifiedとする。
SendMessageBatch以外のClientErrorをSQS応答として分類しない。
共通型は再試行可否や配信停止方針を持たない。

送信前に本文を構築し、SDK providerから資格情報を取得・更新して固定した後、その資格情報でSQSクライアントを作成する。
送信対象がある各publish_batchでクライアントを1つ生成・終了し、SQSのtotal_max_attempts=1を固定する。
テスト用client_factoryも、確定済み資格情報・単一試行・毎回独立したクライアントという同じ契約を満たす必要がある。
資格情報取得先への通信失敗はcredentials_retrieval_failedであり、SQSのtransport失敗ではない。
HttpTransportFailureの到達可能性はその単一通信試行の情報で、過去の送信やconsumerの処理完了を保証しない。

通常例外は操作境界で受け止め、元例外はcauseに保持するが、SDK自由文・本文・資格情報・送信先URLを例外文面へ出さない。
phaseはinitialize、prepare_event、resolve_credentials、send、classify_failureを区別し、終了失敗は専用のPublishCleanupErrorで表す。
分類処理失敗時は元例外と分類失敗の型を保持し、終了処理の失敗は先行する送信失敗を上書きしない。
cleanupの失敗は送信処理の後に起きるため、未送信の根拠にしてはならない。受付成功・送信失敗の結果を維持し、SQS Publisherがcloseの通常失敗を診断ログへ記録してから結果を返す。BatchPublishResultにはイベントの結果だけを保持する。
BaseExceptionによるキャンセル・終了は通常失敗へ変換しない。
Logfireの既存export秘匿処理を維持し、イベント単位の停止記録・通知とevent_idとの関連付けは既存の配信失敗処理が担当する。クライアント終了失敗の診断はSQS Publisher内で完結する。

単体検証はAWSコード表、未知・不正metadata、資格情報の取得・更新失敗、SDKの単一試行、例外の診断情報、終了処理、Logfire exportの秘匿を対象とする。

## バッチ送信契約

Problem: 複数イベントをまとめて送り、HTTP応答と個別の受付結果を区別する。
Evidence: 既存EventEnvelope・資格情報確定処理・共通PublishError、および[AWS SendMessageBatch仕様](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessageBatch.html)に従う。

### 入力と結果

- 同期EventPublisher.publish_batch(envelopes: Sequence[EventEnvelope]) -> BatchPublishResultへ統一し、単件publishは残さない。
- 結果はfrozen dataclassのPublishSucceeded(event_id)、PublishFailed(event_id, error)、BatchPublishResult(results)とする。
- 個別結果は構築時にevent_idがUUIDであることを検証し、PublishFailedはerrorがPublishErrorであることも保証する。BatchPublishResultはresultsがtuple、各要素がPublishSucceededまたはPublishFailedであることを構築時に保証する。型違反は外部の値を含まないTypeErrorで拒否し、暗黙変換しない。例外内部のフィールドは追加検証しない。
- 結果型自身は送信対象との件数・ID・順序の一致を検証しない。結果の空・重複やSQS固有の件数上限も構築条件に追加せず、呼び出し側の対応照合と既存の送信契約で扱う。
- 正常に戻る場合は入力順のtupleに各イベントの結果を必ず1つ保持する。成功はSQS受付を意味し、DB更新やconsumer完了は意味しない。
- 結果のreprに例外を展開しない。共通PublishErrorの診断情報は保持するが、結果型自身はログ・通知を出さない。
- 空・11件以上・重複event_idはValueError、Sequence/EventEnvelopeおよびそのトップレベルフィールドの型違反はTypeErrorで、クライアント生成前に拒否する。
- schema_versionはboolを除く整数とする。payload内部は既存のJSON化可能性だけを検証し、イベントschemaの追加検証は行わない。
- イベントごとの既知不正はPublishEventInvalidError、本文準備中の通常の想定外例外はprepare_eventのPublishUnexpectedErrorとして、そのイベントだけを除外する。
- 既存形式のJSON本文をUTF-8にしたバイト数で計測する。個別本文が1,048,576 bytes超ならmessage_too_largeとし、送信しない。
- 送信可能な本文の合計が1,048,576 bytes超なら呼び出し全体をValueErrorで拒否し、結果も返さない。自動分割しない。
- 全件が個別失敗なら資格情報取得をせず結果を返す。正常イベントがあれば、資格情報を確定してからSendMessageBatchを最大1回呼ぶ。
- EntriesのIdにはstr(event_id)を使い、本文・再送内容は維持する。Idは応答対応付け用で、重複排除を意味しない。

### 応答の分類と診断

- 全体のSDK例外は既存分類を使って送信対象すべてへ反映し、送信前のイベント不正は上書きしない。
- 資格情報取得失敗は既存の設定理由へ変換され、資格情報取得先への通信をSQSのtransport失敗として扱わない。
- Successful/Failedは省略時に空リストとして扱い、存在する場合はリストであることを要求する。
- 応答内のIDが送信対象と一致し、各IDが成功・失敗を通してちょうど1回現れることを分類前に検証する。
- 成功はId/MessageIdの非空文字列と32桁のASCII十六進文字列のMD5OfMessageBody、失敗はId/Codeの非空文字列とboolのSenderFaultを要求する。応答を型へ変換し、ID照合後に送信本文のMD5と比較する。
- 構造・対応付け違反は理由と項目を持つInvalidSqsBatchResponseを原因とするPublishResponseInvalidErrorにし、送信対象すべてへ反映する。未受付と断定しない。
- 個別失敗のCodeを全体SDK例外と同じ対応表で分類する。個別status_codeはNone、request_idは全体metadataから取得し、欠損・型違反ならNoneとする。
- SenderFaultだけで再試行を判断せず、未知の個別コードはunclassifiedにする。HTTP 200を個別失敗のstatusとして扱わない。
- BatchEntryIdsNotDistinct・BatchRequestTooLong・EmptyBatchRequest・InvalidBatchEntryId・TooManyEntriesInBatchRequestをrequest_rejectedへ追加する。
- 対応付けが正常な個別応答の分類処理だけが失敗した場合は、そのイベントをclassify_failureのPublishUnexpectedErrorにする。他の受付成功・失敗は維持する。
- 個別分類失敗の原因は自由文を含まないSqsBatchEntryErrorとし、Code/request IDを診断用に保持する。元原因型と分類処理例外型を保持し、SDK Messageは取り込まない。
- closeの通常例外はSQS Publisher内でPublishCleanupErrorへ変換し、warningのoutbox_publish_cleanup_failedを結果返却前に記録する。ログ項目はerror_codeとoriginal_exception_typeだけとし、本文・Queue URL・資格情報・SDK自由文・例外チェーンを出さない。診断変換・ログ出力の通常例外も先行する受付結果・失敗やキャンセルを上書きしない。closeの再試行や通知メトリクスは追加しない。
- BaseExceptionは包括しない。Relayはcleanupを受け取らず、イベントの再試行policyへ渡さない。

Invariants: 送信は単一SDK試行であり、内部再送・自動分割・並列送信・DB更新をしない。ログ出力はSQS応答不正とクライアント終了失敗の診断に限定する。入力のイベントと既存本文形式を変更しない。
Non-goals: 他工程への展開、DB確保・配信状態更新、relay接続、通知、Lambda変更、本番適用は今回含めない。
Done: 入力境界、全体・個別エラー、不正応答、終了失敗、単一SDK試行と秘匿契約がテストで保証される。

Verification（2026-09-08）: lint・format check、全単体テスト5,717件が成功した。単体テストは`uv run pytest tests/ -m "not integration" -x -q`で選択し、続いて`make test-integration`で実DBテスト1,220件成功・22件skipを確認した。relay接続・AWS上での実送信・本番適用は未実施。

## SQS応答の型定義と本文の整合性確認

Problem: 応答の形式確認と送信IDの照合を分離し、SQSの受信本文が送信本文と一致することをチェックサムで確認する。
Evidence: [AWS SendMessageBatchResultEntry仕様](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessageBatchResultEntry.html)のMD5OfMessageBodyを使用する。Python標準ライブラリのhashlibで計算し、依存は追加しない。

### 形式検証とID照合

- SQS adapter内のfrozen dataclassとしてSqsSuccessfulEntry(id, message_id, md5_of_message_body)、SqsFailedEntry(id, code, sender_fault)、SqsBatchResponse(successful, failed, request_id)を定義する。
- decode_sqs_batch_responseは未検証のSDK応答をobjectとして受け、必須項目と値の形式を検証してからSqsBatchResponseへ変換する。successful/failedはtupleにし、外部の辞書やリストを後続処理へ持ち込まない。
- 個別エントリも_decode_successful_entry・_decode_failed_entryで形式検証して内部型へ変換する。decode成功は形式が正しいことを表し、送信IDとの対応や本文MD5の一致は後続の照合で確認する。
- decodeへの命名整理のローカル検証（2026-09-08）: 検証条件と既存テストの期待値を維持し、lint・format、全単体テスト5,863件、専用Composeプロジェクトvector-test-sqs-decode-89gntqnでDB integration test 1,226件成功・22件skipを確認した。
- MD5は32桁のASCII十六進文字列を要求し、小文字へ正規化する。その他の必須項目・省略可能な一覧とmetadataの扱いは既存契約を維持する。
- SDKのMessageや未使用項目は保持しない。応答型のreprに外部由来の文字列やチェックサムを展開しない。
- validate_sqs_batch_event_idsは形式検証済みの応答と送信ID集合を受け取り、未知・重複・欠落を拒否する。
- MD5形式不正を含む形式違反とID違反は、InvalidSqsBatchResponseを原因とするPublishResponseInvalidErrorとして送信対象全体へ反映する。

### 送信メッセージの型

Problem: SDK用の辞書と期待MD5を別々に渡すことで、送信内容と照合情報の対応が型から読めない。
Evidence: SqsMessage.from_envelopeによる送信準備、SqsMessageのMD5確定、SqsMessageBatchの合計サイズ検査と_send_batch・応答変換の受け渡しを対象とする。

- SQS adapter内のSqsMessage(event_id: UUID, body: str)はfrozen dataclassとし、送る本文と照合用MD5の対応を保つ。body_md5は本文のUTF-8バイト列から構築時に計算し、独立した引数としては受け取らない。bodyとbody_md5はreprに含めない。EventEnvelopeはフィールドに持たない。
- SqsMessage.from_envelope(envelope)は本文の構築と検証を担当し、工程固有のイベント型に依存しない。別工程や未知のevent_typeも本文に保持し、送信先の対応可否は判定しない。成功時はSqsMessageを返し、返値を再検証してSqsMessageかどうかを判定しない。
- 本文の既知不正はPublishEventInvalidErrorとし、理由はinvalid_occurred_at、serialization_failed、message_too_large（UTF-8が1,048,576 bytes超）とする。unsupported_event_typeはPublisherが本文構築前に検出し、本文不正と重なっても優先する。いずれも既存のイベント単位のprepare_event境界で扱い、正常分の送信は続ける。Message内の想定外例外は包まず、publish_batchのprepare_eventが受け止める。
- 本文形式は既存の5項目JSONとする。occurred_atはUTCのZ、小数秒は維持する。JSONの再構築や文字列の正規化は行わない。
- 1件上限の定数はSqsMessage側が持ち、SqsMessageBatchの本文合計サイズ検査も同じ定数を使う。バッチ件数・重複ID・フィールド型・合計サイズは個別メッセージ準備の外とする。
- publish_batchはfrom_envelopeの結果を並べ、正常分からSqsMessageBatchを構築して送信する。SDK用EntriesとMD5辞書を別々に準備・受け渡ししない。
- SDK呼び出しの直前にだけ各メッセージをId/MessageBodyの辞書へ変換する。body_md5はSDKへ送信しない。
- results_from_sqs_batch_responseも同じSqsMessageBatchを受け取り、IDで対応するメッセージを探して照合する。照合対象はEventEnvelopeではなく、送ったSqsMessageとする。

Invariants: 本文・MD5計算・件数/サイズ制限・個別結果・送信回数・資格情報・終了処理の契約を維持する。
Non-goals: failure handlerの追加、例外処理全体の再設計、DB/relay/通知/インフラの変更は行わない。
Done: from_envelopeが送信準備であり、既存の送信・応答照合と本文とMD5の対応が維持されることを確認できる。

Verification（2026-09-08）: lint・format check、全単体テスト5,802件が成功した。`make test-integration TEST_COMPOSE_PROJECT=vector-test-sqs-from-envelope-20260908 PYTEST_ARGS="-x -q"`で実DBテスト1,226件成功・22件skipを確認した。

### 本文照合と共通失敗

- MessageBodyに渡す文字列を一度構築し、SqsMessageが構築時にそのUTF-8バイト列からhashlib.md5(..., usedforsecurity=False).hexdigest()を計算する。JSONの再構築や文字列の正規化は行わない。
- event_id・body・body_md5をSqsMessageにまとめて応答変換へ渡し、ID照合を通過した成功エントリのMD5を同じメッセージのbody_md5と比較する。失敗エントリは既存の共通コード分類へ渡す。
- 応答変換は応答全体の検証・ID照合・結果の集約を担い、SQS側の_verify_sqs_body_checksumが本文照合によるPublishSucceededまたはPublishFailedの判定を担う。任意のrequest_idは診断用であり、照合の成否に影響しない。
- 計算失敗はそのイベントのprepare_eventのPublishUnexpectedErrorとし、他の正常イベントの送信を妨げない。
- 正常な形式のMD5が不一致なら、そのイベントだけをPublishFailed(event_id, PublishIntegrityError)にする。他イベントの結果と独立した終了失敗の診断は維持する。
- PublishIntegrityErrorのCODEはpublish_integrity_error、PublishIntegrityReasonはBODY_CHECKSUM_MISMATCH=body_checksum_mismatchとする。
- 共通型の固定説明文は「送信本文と、送信先が受け取った本文のチェックサムが一致しません。」とし、AWS依存・再試行方針を含めない。
- 例外はreasonと任意のrequest_idだけを保持し、本文・期待MD5・受信MD5・生応答を保持しない。request IDは例外文面とreprに含めない。
- policyは明示的にNonRetryable(NON_RETRYABLE_FAILURE)を返す。5回目以降もretry_exhaustedに上書きせず、DBの停止理由表現は変更しない。

### 停止ログ

handlerはDeliveryStoppedの確定後にrecord_publish_failureへ停止理由を渡し、記録関数はerror_codeとerror_reasonに加え、次の固定文をerror_messageに記録する。

> 送信した本文と、送信先が受け取った本文のチェックサムが一致しません。送信先には受付済みの可能性があるため、このイベントの自動再試行を停止しました。

- SQS固有の説明文は記録処理側に置き、SDK自由文や例外のmessageから取得しない。
- 本文・チェックサム・request IDはログに出さない。設定修復のメトリクスとSlack通知の対象6理由は変更しない。
- 再試行予約・更新なし・commit失敗では本文不一致の停止メッセージを出さない。

Invariants: MD5照合は受信本文の整合性確認であり、暗号学的な真正性・consumerの処理完了・処理阻止を保証しない。不一致でもSQS受付済みの可能性があり、自動再送しない。送信本文、資格情報確定、単一試行、個別結果、終了処理の境界は維持する。
Non-goals: DB schema、relay接続、consumer、通知条件、AWS設定、本番適用は変更しない。
Done: 形式検証・ID照合・MD5一致/不一致・固定ログ・秘匿・停止policyと既存送信契約を単体テストで保証し、DB停止確定後だけ記録されることを実DBテストで確認する。

Verification（2026-09-08）: lint・format check、全単体テスト5,790件が成功した。`make test-integration TEST_COMPOSE_PROJECT=vector-test-sqs-integrity-20260908 PYTEST_ARGS="-x -q"`で独立したテスト環境を使用し、実DBテスト1,223件成功・22件skipと終了時の環境削除を確認した。relay接続・本番適用・実際の通知確認は未実施。

## 送信対象の確保と試行上限到達の停止

Problem: 指定したイベントタイプの送信可能行を発生日時の古い順に確保し、確保回数が上限に達した担当不在のイベントは再送せず停止する。
Evidence: 既存OutboxDeliveryRepository、outbox_eventsの状態・lease制約、pendingインデックス、確保とトランザクションのDBテストに従う。

### 共通の試行上限と対象範囲

- `publish_retry_policy.MAX_PUBLISH_ATTEMPTS = 5`を確保・停止・送信失敗後の再試行判断で共有する。
- 回数は初回を含む、commitされた配信試行の確保回数であり、実際のネットワーク送信回数ではない。
- `event_type`は必須の完全一致条件とし、全イベントを対象とする省略値は持たない。後続のrelayは`article.assessed_in_scope`を渡す。
- 発生日時の下限は設けず、古いイベントも対象に含む。他工程の行は確保・停止の両方から除外する。

### APIと更新条件

`claim_ready_batch(*, event_type: str, lease_duration: timedelta, limit: int = 10) -> list[ClaimedOutboxEvent]`

- 未配信・未停止・再試行時刻到来・leaseなしまたは期限切れ・確保前の試行回数が5回未満をDB側で判定する。
- `occurred_at ASC`で候補を選び、件数上限と`FOR UPDATE SKIP LOCKED`を適用して、1つのSQLでlease設定と試行回数の加算を行う。
- 確保前4回は確保後5回となり、確保前5回以上は確保しない。本文・発生日時・再試行予定日時など他の列は変更しない。
- `RETURNING`の順序に依存せず、取得したoccurred_atで返却結果も整列する。同じ発生日時のイベント同士の選択・返却順は保証せず、ID順も要求しない。next_attempt_atは送信可能時刻の判定だけに使う。

`stop_deliveries_at_attempt_limit(*, event_type: str, limit: int = 100) -> list[UUID]`

- 未配信・未停止・試行回数5回以上・leaseなしまたは期限切れの行を対象とし、next_attempt_atが未来でも停止する。
- 確保と同じ順序・ロック方式で候補を制限し、1つのSQLで停止日時をDB現在時刻、停止理由を`NonRetryableReason.RETRY_EXHAUSTED.value`にし、leaseの2列をNULLへ戻す。
- 試行回数、本文、イベント発生日時、再試行予定日時、配信成功日時は変更しない。更新したevent_idを候補と同じ順序で返す。
- 担当者による`stop_delivery`は、有効なleaseとtoken一致を要求する。上限到達による停止は、leaseなしまたは期限切れを要求する。対象条件は各経路に残し、停止日時・理由・lease解除の更新定義をrepository内で共通化する。
- 定期実行は呼び出し元の責任とし、この操作は1回につき既定で最大100件を停止する。SQSのバッチ送信とは別の処理であり、PublishErrorを作らず、PublishFailureHandlerやログ・通知には接続しない。
- 100件を超えて未停止のまま残った行も、確保条件によって6回目の確保には入らない。

### 入力・トランザクションの境界

- event_typeの型違反はTypeError、空・空白のみはValueErrorとし、文字列を自動補正しない。
- limitはboolを除く整数を要求し、型違反はTypeError、0以下はDB操作なしで空リストを返す。
- lease_durationはtimedelta以外をTypeError、0以下をValueErrorとし、limitが0以下でも検証する。
- 件数は初期値であり呼び出し元が変更できる。repositoryにSQSの10件というリクエスト上限は持ち込まない。
- 時刻判定はstatement_timestamp()を使い、事前SELECTやアプリ時刻による判定を加えない。ロック中の行を避けるため、全体の厳密なFIFOではなく確保可能な行の中で発生日時の古い順となる。
- repositoryはcommit・rollbackを行わず、返却値は未commitの更新結果である。DB例外は送信失敗に変換せず伝播する。
- 有効な5回目のleaseやロック中の行を停止処理で上書きしない。停止後は古いtokenによる結果更新を拒否する。

Invariants: 同じ上限を参照し、対象タイプの境界・lease所有権・配信済み／停止済みの保護・短いトランザクションの責務を維持する。
Non-goals: relay接続、送信、ログ・通知、Lambda変更、DB schema・インデックス・依存の変更、本番適用、重複防止の実装は含めない。
Done: 範囲・回数・順序・初期件数・更新列・同時操作・commit／rollbackとDB例外の境界をテストで保証する。

テストは発生日時による選択・返却順、同時刻の順序非保証、確保件数上限、停止件数上限、未停止の上限到達行の再確保防止、次回停止への引き継ぎを独立して検証する。停止件数のテストでは102件中100件を停止し、残り2件のDB状態が変更されないことを確認する。

Verification（2026-09-08、テスト再配置・停止更新共通化前）: lint・format check、全単体テスト5,941件が成功した。`uv run pytest tests/ -m "not integration" -x -q`の後、`make test-integration TEST_COMPOSE_PROJECT=vector-test-outbox-occurrence-20260908 PYTEST_ARGS="-x -q"`で実DBテスト1,269件成功・22件skipを確認し、専用テスト環境の終了時削除も完了した。relay接続・本番適用は未実施。

最終変更後の検証: 確保・配信結果更新・上限到達停止・トランザクション・failure handlerの関連単体テスト38件、実DBテスト110件が成功した。lint・format checkも成功した。利用者の指定により全テストの再実行は行っていない。

lease期間・1起動あたりの処理量・確保と停止の呼び出し順は後述のrelay本体の契約に従う。実行間隔の設定とLambda入口への接続、古いイベントと既存パイプラインの重複防止確認は本番有効化前に行う。

## Outbox relay本体（配線スライス①）

Problem: ベクトル生成向けイベントの確保・送信・結果記録を接続し、送信失敗とDB障害を区別して扱う。
Evidence: EventPublisher、PublishFailureHandler、OutboxDeliveryRepository、caller_managed_session_factoryの既存契約を使用する。

`OutboxRelay(session_factory, publisher, failure_handler).run_once() -> None`は非同期APIとし、handlerには同じDBを使うsession factoryを渡す。

- 対象は`ArticleAssessedInScope.EVENT_TYPE`に固定する。1回の実行は上限到達の停止100件、確保10件、送信最大1回までとし、lease期間は150秒とする。
- 専用sessionで上限到達を停止してcommit・終了し、別sessionで確保してcommit・終了する。確保0件なら送信せず正常終了する。
- 確保結果を`EventEnvelope.from_claimed`で送信内容へ変換し、同期publisherを直列に1回だけ呼ぶ。送信中はDB sessionを保持しない。
- publisherの戻り値がBatchPublishResultであることを確認し、件数・入力順のevent_idを全件照合してから結果を適用する。結果型が構築時に保証する内部構造は再検証しない。契約違反は外部の値を含まないTypeErrorまたはValueErrorで伝播する。
- 成功はイベント単位のsessionでmark_publishedを呼び、Trueならcommit、Falseならrollbackして終了する。失敗は対応する確保情報とPublishFailedを既存handlerへ渡す。更新条件不一致でも後続の結果記録を続ける。
- handlerによる停止確定後の記録を再度呼ばない。上限到達の整理に架空のPublishErrorや送信失敗通知を追加しない。
- 更新・commit・session終了の例外は実行全体へ伝播し、後続の結果記録を中断する。既存factoryのDB例外変換を使い、PublishErrorへ変換しない。確定済みの変更を取り消さず、未記録の確保情報はlease期限切れ後の既存処理に委ねる。
- Envelope構築・publisher呼び出し自体の失敗でも自動分割・その場での再送・合成したイベント別失敗を追加しない。BaseExceptionを包括しない。
- クライアントの終了と終了失敗の診断はSQS Publisher内で完結し、Relayはイベント結果の照合とDB反映を担当する。cleanupの戻り値・ログ出力・finally処理を持たない。

Invariants: 正常終了は今回の処理の終了を意味し、全件送信成功やconsumerの完了を意味しない。受付後にDB記録が失敗した場合の重複送信は既存契約どおり許容する。
Non-goals: Lambda入口、Terraform、通信timeout、定期起動、本番適用、consumer、既存Taskiqの切り替えは変更しない。Lambda120秒の反映は次のスライスとする。
Done: 実DBと差し替えpublisherで、確定順序・部分失敗・更新なし・障害時の中断を検証する。

Verification（2026-09-08）: relay本体とテストのlint・format checkが成功した。テストの責任整理前には、Outboxの関連単体テスト609件と実DBテスト190件が成功した。責任整理後は、`make test-integration TEST_COMPOSE_PROJECT=vector-test-relay-responsibility-20260908 PYTEST_ARGS="tests/outbox/test_relay.py -x -q"`でrelayの実DBテスト36件が成功した。その後の変更はコメントのみで、lint・format checkを確認した。利用者の指定により全テストは再実行していない。Lambda接続・本番適用・AWSへの実送信は未実施。

## 再試行・停止ポリシー（スライス①）

Problem: publisherが返す原因から、再試行・停止を判断する規則を一箇所に定義する。
Evidence: publish_errorsの共通分類と、claim_ready_batchが確保時にattempt_countを加算する契約に従う。

publish_retry_policy.decide_publish_retry(error, attempt_count, jitter)は、副作用のない判断関数とする。
結果は不変のRetryable(delay)またはNonRetryable(reason)で返す。
Retryableは原因と回数上限を確認済みの最終判断であり、次回の配信試行までの待ち時間を必ず持つ。
RetryableとNonRetryableは確定した判断だけを保持し、原因の判定メソッドを持たない。decide_publish_retryが入力検証、原因判定、回数上限の確認、待ち時間計算と結果の生成を取りまとめる。
再試行対象外の原因は原因別の停止理由を優先し、再試行対象でも回数上限に達した場合はretry_exhaustedを返す。それ以外は計算した待ち時間を持つRetryableを返す。原因判定と停止理由の分類はポリシー内の非公開関数が担い、待ち時間の計算関数はtimedeltaだけを返す。中間分類専用の結果型や、Noneによる再試行可否の表現は使用しない。
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
| integrityのbody_checksum_mismatch | non_retryable_failureで停止 |
| unexpected・未対応のPublishError | unexpected_failureで停止 |

再試行対象は初回を含め最大5回とし、1〜4回目の失敗後の基準待ち時間を30秒・2分・10分・30分とする。
実際の待ち時間は基準時間 × (0.8 + 0.4 × jitter)で、jitterは呼び出し元が渡す0〜1の有限値とする。
5回目以降はretry_exhaustedで停止するが、即停止対象の理由は上書きしない。
attempt_countは確保後の1以上の整数で、実送信回数ではなく配信試行の確保回数を意味する。
非整数の回数・非数値のjitter（boolを含む）はTypeError、値の範囲違反はValueErrorとする。
停止対象でもこれらの入力条件を満たす必要がある。

Invariants: 時刻取得・乱数生成・AWSコードの再解釈・DB更新・通知を行わず、同じ入力に同じ結果を返す。
到達可能性がTrueでも重複許容の再試行方針を変えず、入力の診断情報を変更しない。
PublishCleanupErrorはPublishErrorを継承せず、再試行policyでは他の非PublishErrorと同じくTypeErrorで拒否し、relay側で別経路として扱う。

Non-goals: failure handlerによる状態更新、Slack通知、relay接続、停止後の再開は後続スライスとする。
Done: 全分類、proxy境界、試行上限、jitter境界、不正入力、判断の不変性を単体テストで保証する。
この段階では稼働中の配信処理の再試行動作は変更しない。

## 配信失敗の状態更新（スライス②）

Problem: PublishFailedを入口として、policyの判断・DBへの確定・確定後の停止記録を取りまとめる。
Evidence: OutboxDeliveryRepositoryは更新成否をboolで返し、トランザクションの確定は呼び出し元が担う。

PublishFailureHandler.handle(event=ClaimedOutboxEvent, failure=PublishFailed)は、イベント単位の短いトランザクションと停止確定後の記録を管理する。
failureがPublishFailed以外ならTypeError、event_idが確保済みイベントと一致しなければValueErrorとして、jitter生成・DB操作・記録の前に拒否する。
session factoryはcaller_managed_session_factoryと互換のsession開閉専用factoryを注入する。
jitter生成関数は既定でrandom.randomを使い、テストでは固定値を注入する。

1. jitterを1回生成し、確保後のattempt_countとfailure.errorをpolicyへ渡す。
2. 判断が得られてからsessionを開く。
3. Retryableはschedule_retry、NonRetryableはstop_deliveryで条件付きUPDATEを行う。
4. 更新成功時はcommit、更新なしはrollbackする。
5. session終了後、DeliveryStoppedに限りrecord_publish_failure(event, error=failure.error, stop_reason=outcome.reason)を1回呼ぶ。
6. RetryScheduled(delay)、DeliveryStopped(reason)、DeliveryUpdateSkippedのいずれかの不変な結果を返す。

結果型はpublish_failure_handler.pyに置き、recordingは結果型に依存しない。

delivery_stop_reasonにはNonRetryableReason.valueのみ保存し、失敗の自由文や本文を含めない。
元の原因・診断情報はfailure.errorに残し、停止確定後の記録でevent_idと関連付ける。
失敗分類はpublisher、再試行判断はpolicy、記録内容と出力障害の処理はrecordingが担う。
更新なしはtoken不一致・lease失効・処理済み・イベント不在などを含み、理由を推測しない。
DB時刻と更新権限はrepositoryへ委譲し、事前SELECTやhandler側での時刻比較を追加しない。

Invariants: 本文・試行回数・published_atを変更せず、lease解除は既存の更新処理に任せる。
policyの拒否やjitter生成失敗はsessionを開かず伝播する。
DB更新・commit・session終了が失敗した場合は記録関数を呼ばず、成功結果を返さず、DB例外は既存session factoryの変換に従い、PublishErrorへ再分類しない。
未確定の変更はsession終了時に取り消すが、commit後の終了失敗で確定済み変更を取り消したとは扱わない。
キャンセルやプロセス終了は通常の配信失敗として捕捉しない。

Non-goals: relay接続、成功処理、停止後の再開、schemaや既存policy・repository・publisher、通知対象の変更は含めない。
Done: 別sessionから予約・停止の確定を確認し、更新不能・再実行・更新障害・commit障害・終了障害の結果を実DBテストで保証する。

## 設定修復が必要な送信失敗の記録（スライス③）

Problem: 人の設定修復が必要な送信停止だけを、ログとメトリクスへ書く。
Evidence: 既存EMF出力とCloudWatch Alarm → SNS → Amazon Q → Slackの通知経路は記録の先にあり、モジュールの仕事ではない。

requires_publish_configuration_fixは、configurationのmissing_credentials・incomplete_credentials・missing_regionと、serviceのauthentication_failed・access_denied・destination_not_foundだけをメトリクス対象にする。
credentials_retrieval_failed、暗号化関連、通信失敗、イベント不正、未分類・想定外はメトリクスを書かない。
NonRetryableReasonだけでは設定修復が必要か判断しない。

record_publish_failure(event, error, stop_reason=NonRetryableReason)は、handlerが停止確定を確認した後に呼び、outbox_delivery_stoppedをinfoレベルで記録する。
RetryScheduled・DeliveryUpdateSkippedではhandlerが記録関数を呼ばない。
ログ項目はevent_id、attempt_count、stop_reason、error_code、requires_configuration_fixと、該当時のerror_reason・transport_kind・phase・original_exception_type・classification_exception_typeに限定する。
本文・資格情報・Queue URL・SDKの自由文や生のサービスコード・例外チェーンを出力しない。
設定修復が必要な停止だけ、Vector/Pipelineのoutbox_publish_configuration_failureへCount=1をEMFで出力する。
dimensionは空とし、個別イベント・原因ごとの系列を増やさない。

AlarmはOutboxのSQS送信全体で1つ、60秒のSumが1以上、evaluation_periods=1、datapoints_to_alarm=1、欠損はnotBreachingとする。
既存のalerts SNS topicへALARM遷移だけ通知し、OK・INSUFFICIENT_DATAの通知は設定しない。
新しい発生がなくなったことは修復完了を意味しない。

Invariants: 記録関数はhandler内部でcommit・session終了に成功した後に呼び、RetryScheduled・DeliveryUpdateSkippedでは出力しない。
ログとメトリクスは独立して試行し、通常の出力失敗を配信処理へ返さない。
出力失敗は固定メッセージoutbox_failure_recording_failedと例外型だけで記録を試み、その失敗を再帰的に記録しない。
BaseExceptionは捕捉しない。
DB確定と記録出力は非原子的であり、間のプロセス終了による欠落は保証対象外とする。
このメトリクスは厳密なユニーク停止件数ではなく、記録の再呼び出しやEMFの重複を許容する発生検知用とする。

Non-goals: relayへの配線、通知の永続化・再配信、本番apply、Slack到達確認、IAM権限・通知先・依存追加は行わない。
Done: 全共通reasonの対象判定、EMFとAlarmの契約一致、出力秘匿、sink障害の分離、実DBで停止確定後だけ記録されることを検証する。

Verification（2026-09-08）: lint・format check、全単体テスト5,837件が成功した。`make test-integration TEST_COMPOSE_PROJECT=vector-test-publish-recording-20260908 PYTEST_ARGS="-x -q"`で実DBテスト1,226件成功・22件skipを確認した。


### handlerの入口と停止記録の統合

Evidence: PublishFailedはイベントIDと共通PublishErrorを持ち、既存recordingは通常の出力失敗を独立して処理する。
Invariants: DB確定前・session終了失敗・更新なしでは記録せず、出力障害によって確定済みの停止を取り消さない。
Done: 型・IDの拒否、停止確定後の記録順序、DB障害時の非記録、出力障害時の確定状態維持をテストで保証する。
relay接続・AWS上の送信・本番適用・Slack到達確認は未実施。

ローカル検証（2026-09-08）: lint・format、全単体テスト5,794件、DB integration test 1,226件（22件skip）が成功。


### 発生段階ごとの例外変換の統合

Problem: publisherとクライアント生成処理で重複していた、例外分類・想定外への変換・原因保持を揃える。
Evidence: sqs_error_mappingの既存AWS分類と、資格情報取得・送信・終了を区別する既存テストを使用する。
publish_error_from_exception(exc, phase=PublishPhase)をsqs_error_mapping.pyの例外からの変換入口とし、必ずPublishErrorを返す。
既存PublishErrorはそのまま返す。終了失敗はpublish_cleanup_error_from_exceptionでPublishCleanupErrorへ変換し、既存PublishCleanupErrorはそのまま返す。
RESOLVE_CREDENTIALSは設定不足の専用理由を優先し、その他のSDK失敗をcredentials_retrieval_failedにする。
INITIALIZEは設定不足、SENDは既存SQS例外分類を使い、PREPARE_EVENTなどの未分類例外は発生段階の想定外とする。
分類処理の通常例外はclassify_failureとし、元の例外型・分類処理例外型・元の原因を維持する。
Invariants: 終了処理の通常失敗はSQS Publisher内で記録し、イベントの送信結果を上書きしない。
既存PublishErrorの原因チェーン、本文、試行回数、送信対象、応答の個別失敗の扱いを維持し、BaseExceptionを捕捉しない。
Non-goals: 結果型・再試行policy・failure handler・個別応答分類・relayの変更、新しいファイルや依存の追加は行わない。
Done: 段階別の分類、既存エラーの維持、分類失敗、終了失敗、診断情報の秘匿をテストで保証する。

例外変換統合のローカル検証（2026-09-08）: lint・format、全単体テスト5,837件、DB integration test 1,226件（22件skip）が成功。relay接続・AWS実送信・本番適用は未実施。


### 失敗が影響するイベントへの結果の割り当て

Problem: 変換済み例外をraiseして再捕捉する往復をなくし、失敗した処理が対象イベントを指定する。
Evidence: 既存の全体失敗・個別失敗・不正応答・終了失敗のテストと、共通例外変換の契約を使用する。
sqs_publisher.pyのfailed_results(batch, error)は指定されたバッチの対象だけにPublishFailedを作り、分類や再試行判断を行わない。
本文作成失敗は該当イベントだけ、クライアント生成・送信全体・応答全体の検証失敗は準備済み送信対象全件へ反映する。
個別応答の失敗・本文不一致は既存応答処理が該当イベントだけに反映する。
クライアント生成、SDK送信、応答処理はそれぞれの境界で例外を共通変換し、その場で結果を作る。
Invariants: 終了処理はクライアント生成成功後に実行し、通常の終了失敗をSQS Publisher内の診断ログへ分離する。
既存の失敗原因・部分成功・入力順・本文・送信回数・BaseExceptionの伝播を維持する。
Non-goals: 新しいファイル・クラス・テスト、例外分類・再試行policy・failure handler・relayの変更は行わない。
Done: 既存テストによる振る舞いの検証と、lint・format・全単体テスト・DB integration testが成功する。

結果割り当て整理のローカル検証（2026-09-08）: テストの追加・変更なしで、lint・format、全単体テスト5,837件、DB integration test 1,226件（22件skip）が成功。relay接続・AWS実送信・本番適用は未実施。


### 終了失敗の専用型

Problem: 終了失敗をPublishUnexpectedErrorのphaseで表すと送信失敗との区別が読み取りにくく、変換関数にoverloadが必要になる。
Evidence: 終了失敗はイベントの受付結果を上書きせず、再試行policyへ渡さない既存契約を持つ。
PublishCleanupErrorはVectorDomainErrorを直接継承し、CODE=publish_cleanup_errorとoriginal_exception_typeを安全な診断情報として持つ。
PublishErrorの継承関係から分離し、SQS Publisher内の診断に使用する。BatchPublishResultには含めない。
publish_cleanup_error_from_exceptionは元の例外をcauseとして保持し、送信失敗の分類は行わない。
PublishPhase.CLEANUPとoverloadを削除し、終了失敗を型で識別する。
Invariants: 既存の受付成功・個別失敗・原因チェーン・自由文の秘匿とBaseExceptionの伝播を維持する。
Non-goals: 再試行ルール・通知対象・relay・DB・依存の変更、新しいファイルの追加は行わない。
Done: 既存テストを専用型の契約に同期し、結果維持・再試行対象外・Logfire秘匿を検証する。

終了失敗の専用型のローカル検証（2026-09-08）: lint・format、全単体テスト5,837件、DB integration test 1,226件（22件skip）が成功。既存テストを新契約に同期し、relay接続・AWS実送信・本番適用は未実施。


### SQS失敗からPublishErrorへの変換入口

Problem: 個別応答の分類処理失敗を応答処理側が包んでいたため、PublishErrorへの変換責務が分散していた。
Evidence: 既存のAWSコード対応表、例外の段階別分類、部分成功・分類処理失敗・終了失敗のテストを使用する。

- sqs_error_mapping.pyが変換を所有し、公開入口はpublish_error_from_exception(exc, phase)とpublish_error_from_sqs_entry(code, request_id)とする。
- 例外からの入口はSDK例外と処理中の通常例外を受け、既存PublishErrorの同一性・発生段階・元の原因を維持する。
- 個別失敗の入口は形式検証済みのCode/request IDを受け、既存対応表のPublishServiceErrorまたは分類処理失敗のPublishUnexpectedErrorを返す。status_codeはNone、未知コードはunclassifiedとする。
- 両入口の通常の戻り値はPublishErrorとし、Noneを返さない。設定・SDK例外・サービスコード別の分類関数は内部関数にし、内部分類のNoneは例外からの入口で想定外へ変換する。
- 個別分類処理の通常例外はclassify_failureとし、元の個別失敗情報と分類処理例外型を保持する。SqsBatchEntryErrorを変換モジュールへ移し、診断上の完全修飾型名はapp.outbox.sqs_error_mapping.SqsBatchEntryErrorとする。互換用の別名は設けない。
- sqs_batch_response.pyは形式検証・ID照合・本文比較とイベントへの結果の対応付けを担当し、個別失敗の分類を公開入口へ委譲する。チェックサム不一致を検出した箇所でのPublishIntegrityError生成は維持する。
- 終了失敗は別入口publish_cleanup_error_from_exceptionでPublishCleanupErrorへ変換し、送信失敗とは分離する。

Invariants: 分類結果・部分成功・原因情報・自由文の秘匿を維持し、BaseExceptionを捕捉しない。
Non-goals: 送信・応答検証・再試行policy・DB・通知・relay・本番設定の振る舞い変更、新しいファイルや依存の追加は行わない。
Done: 呼び出し側の個別失敗の変換分岐がなくなり、公開入口による全コードの分類、分類失敗、診断情報、終了失敗の分離と部分成功が検証できる。

変換入口整理のローカル検証（2026-09-08）: lint・format、全単体テスト5,843件が成功。`make test-integration TEST_COMPOSE_PROJECT=vector-test-sqs-error-entry-59k8jpog PYTEST_ARGS="-x -q"`でDB integration test 1,226件成功・22件skipを確認した。relay接続・AWS実送信・本番適用は未実施。


### SqsMessageBatchによる送信単位

Problem: 1回で送るまとまりの件数・ID重複・合計サイズの条件を、送信処理から分離して型に定義する。
Evidence: 既存SqsMessage、publisherの入力検証・本文準備と、送信・失敗結果・応答照合の受け渡しを対象とする。

- sqs_message_batch.pyにfrozen・slotsのSqsMessageBatch(messages: tuple[SqsMessage, ...])を定義し、保持するフィールドはmessagesだけとする。
- 構築時にtupleと要素型を確認し、次に1〜10件、event_idの重複なし、UTF-8本文合計がMAX_MESSAGE_BYTES以内を順に確認する。型違反はTypeError、件数・重複・サイズ違反はValueErrorとする。
- 件数上限は同モジュールのMAX_BATCH_MESSAGES=10を使い、publisherの入力件数検証も参照する。本文上限は既存のMAX_MESSAGE_BYTESを使う。
- メッセージの順序・本文・MD5を維持し、JSON化やMD5計算を追加しない。通常表示に本文・MD5を出さず、検証エラーに本文・イベントIDを埋め込まない。
- 公開publish_batch(envelopes)は維持する。入力件数・型・重複IDは準備前に検証し、11件の入力を不正イベントの除外によって受け入れない。
- 準備に成功したメッセージから、個別例外変換の外でバッチを構築する。合計超過は従来どおり呼び出し全体のValueErrorとなり、クライアントを生成しない。
- 正常なメッセージが0件ならバッチを構築せず、準備失敗の結果だけを返す。
- _send_batch・_send_messages・failed_results・results_from_sqs_batch_responseはSqsMessageBatchを受け取り、batch.messagesを使用する。SDK用Entriesは送信直前に作り、送信先はpublisherが保持する。

Invariants: EventEnvelope・SqsMessage・本文形式を変更せず、単一SDK試行・入力順の結果・部分成功・終了失敗の分離を維持する。
Non-goals: 自動分割、送信先や公開APIの追加、DB・relay・通知・再試行policyの変更は行わない。
Done: バッチ構築時の不変条件と、既存の送信・応答照合の契約を単体テストとDB integration testで保証する。

SqsMessageBatchのローカル検証（2026-09-08）: lint・format、全単体テスト5,863件が成功。`make test-integration TEST_COMPOSE_PROJECT=vector-test-sqs-message-batch-tryr1ux9 PYTEST_ARGS="-x -q"`でDB integration test 1,226件成功・22件skipを確認した。relay接続・AWS実送信・本番適用は未実施。


### 応答不正の理由付き失敗契約

Problem: decodeとID照合で検出した違反が同じ想定外エラーになり、何が不正だったかを呼び出し元で確認できなかった。
Evidence: 既存の応答形式検証・ID照合・本文MD5比較、エラーマッピング、停止policy、停止確定後の記録を対象とする。

- PublishResponseInvalidError(CODE=publish_response_invalid)はPublishErrorを継承し、共通のreasonだけを保持する。SQS固有のfieldは持たず、SAFE_ATTRSもCODEとreasonだけとする。
- PublishResponseInvalidReasonはinvalid_type、missing_required_field、empty_required_field、invalid_checksum_format、unknown_entry_id、duplicate_entry_id、missing_entry_idとする。項目が存在してNoneならinvalid_type、キー自体の欠落ならmissing_required_field、必須文字列が空ならempty_required_fieldとする。
- SQS側のsqs_response_errors.pyに定義するSqsResponseFieldはresponse、successful_entries、failed_entries、successful_entry、failed_entry、entry_id、message_id、body_checksum、error_code、sender_faultとする。実際のID、本文、チェックサム、SDK自由文、request IDは取り込まない。
- InvalidSqsBatchResponseをsqs_response_errors.pyに置き、decode・ID照合が共有するPublishResponseInvalidReasonとSQS固有のSqsResponseFieldを指定して送出する。旧PublishResponseFieldの互換名は設けない。応答検証とマッピングの循環依存を作らず、違反理由を二重定義しない。
- publish_error_from_exceptionのsend境界でPublishResponseInvalidErrorへ変換し、共通エラーにはreasonを保持し、SQSの診断詳細は元の例外をcauseとして維持する。共通のポリシーと記録処理はcause内のSQS情報を参照しない。その他の段階で発生した場合は既存どおり想定外とする。
- 形式・ID対応の違反は送信対象全件を失敗とし、受付されなかったとは断定しない。正常形式のMD5が送信本文と異なる場合は、既存のPublishIntegrityErrorとして該当イベントだけに反映する。
- 変換処理自体の通常例外は既存のclassify_failureのPublishUnexpectedErrorとし、元原因と分類処理例外型を保持する。
- 全reasonを自動再試行の対象外とし、停止区分はunexpected_failureからnon_retryable_failureへ変更する。元の違反理由は入力のPublishErrorに保持する。
- SQS Publisherは応答検証のInvalidSqsBatchResponseを捕捉した際、共通エラーへの変換前にoutbox_sqs_response_invalidをwarningで送信バッチにつき1回記録する。error_reasonとresponse_fieldは列挙値、event_idsは実際に送信したバッチのイベントIDを送信順に文字列リストで保持する。送信前に除外したイベントや応答に混入した外部IDは含めない。
- 診断には本文・チェックサム・Queue URL・資格情報・SDK自由文・例外チェーンを出さない。通常のログ出力失敗は既存の結果・例外変換へ影響させず、再試行や追加メトリクスを発生させない。BaseExceptionは包括しない。正常応答や正常形式のMD5不一致では応答不正の診断を出さない。
- 共通の停止確定後のログにはerror_codeとerror_reasonを記録し、response_fieldは持たせない。SQSの検出時診断と確定後の停止ログはevent_idで関連付ける。設定修復アラームの対象6理由は変更しない。

Invariants: 応答を受け入れる条件、必須項目の検証順、metadata欠損の扱い、本文・入力順・単一SDK試行・既存の部分成功・終了失敗の分離・BaseExceptionの伝播を維持する。
Non-goals: DB schema・relay・通知経路・AWS設定・依存の変更、自動再試行の追加は行わない。
Done: 全違反理由が共通エラーと停止ログまで保持され、秘匿と既存の結果境界をテストで保証し、lint・format・全単体テスト・DB integration testが成功する。

応答不正の失敗契約のローカル検証（2026-09-08）: lint・format、全単体テスト5,916件が成功。`make test-integration TEST_COMPOSE_PROJECT=vector-test-response-invalid-3z0mnr18 PYTEST_ARGS="-x -q"`でDB integration test 1,229件成功・22件skipを確認した。応答不正による停止のcommit成功後の記録、commit失敗・更新なし時の非記録を実DBで検証済み。AWS実送信・本番適用は未実施。
