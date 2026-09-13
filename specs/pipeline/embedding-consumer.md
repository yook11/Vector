# EmbeddingConsumer — SQS受信とベクトル生成

Status: Consumer配置・専用SSM登録済み、SQS受信有効化のコード実装済み・AWS未適用（2026-09-10）

業務上の成功・失敗、再配信設定、Taskiqとの併用方針と、Lambda呼び出し単位の接続・APIキー管理は合意済み。Lambda実行の数値設定と実装詳細は、各スライスの確定項目に分ける。基盤のTerraform実装と実環境での有効化を区別して記録する。

## Problem

`article.assessed_in_scope`をOutboxからSQSへ送る実装はあるが、受信してベクトル生成を実行するconsumerがない。`EmbeddingConsumer`を追加し、既存の業務処理を再利用してSQS受信から結果保存まで接続する。

現在のTaskiq workerと同時に稼働する期間を許容し、重複配信・並行実行はアプリケーションの処理済み判定と条件付き保存で吸収する。

## Evidence

- [OutboxからSQSへの送信契約](./outbox-sqs-message-contract.md)：イベント形式と送信先。
- [ArticleAssessedInScope](../../backend/app/analysis/assessment/events.py)：イベント種別・バージョン・payload。
- [既存Taskiqタスク](../../backend/app/queue/tasks/embedding.py)：Ready構築、Service呼び出し、失敗処理。現在のtask timeoutは60秒。
- [EmbeddingService](../../backend/app/analysis/embedding/service.py)：AI呼び出しと、ベクトル・成功監査の同一トランザクション保存。
- [EmbeddingConsumer](../../backend/app/analysis/embedding/consumer.py)：検証済みpayloadの受信、開始状態の取得、60秒の業務処理、失敗後処理と例外伝播。
- [EmbeddingRepository](../../backend/app/analysis/embedding/repository.py)：生成済み判定と、embeddingがNULLの場合だけ更新する保存処理。
- [worker起動設定](../../backend/supervisord/analysis.conf)：embedding workerの最大同時実行数は1プロセスあたり10。
- [relay基盤](../../infra/aws/outbox_relay.tf)：Standardキュー、relay Lambda、無効状態のScheduler。ConsumerとSQS起動トリガーは配置済み。
- [Consumer基盤](../../infra/aws/embedding_consumer.tf)：専用サブネット・IAM・SSM経路・DLQ・通知。
- [適用手順](../../infra/aws/README.md#embeddingconsumer基盤の追加スライス1)：bootstrap先行・滞留確認・秘密情報登録・後続検証。
- AWS公式：[SQSとLambdaの接続設定](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)、[同時実行制御](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)、[DLQ](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)。

現状はリポジトリとImplementationの時点付き実環境確認に基づく。受信開始と実データ処理の結果は、配置済みの事実と区別して記録する。

## Invariants

- 保存完了とは、ベクトルと成功監査のトランザクションをコミットできたことを指す。
- 「例外なくreturnした」ことだけを根拠にSQSへ成功を返さない。
- 開始時に生成済み、または別の実行が先に保存したことを確認できた場合は対応完了とする。
- 開始時の対象欠損・Ready入力制約違反は、理由付き拒否として受信完了にする。Ready成立後の実行・保存時の不存在は失敗とする。
- 初期実装では、想定内か想定外かを問わず処理失敗をSQSへ返す。失敗記録だけでメッセージを処理済みにしない。
- consumer内部で再配信待ちのsleep、独自の段階的バックオフ、DLQへの直接送信をしない。
- 冪等性の業務上の判定対象は`analyzed_article_id`であり、SQSメッセージIDやevent_idだけでTaskiqとの重複を判定しない。
- 通知・ログ・監査の秘匿を維持し、メッセージ本文やSDK例外の自由文を無制限に記録しない。
- 無料枠ゲートを再導入しない。

## 入力と責務

名称は`EmbeddingConsumer`とする。`article-embedding`キューから、以下のイベントを受け取る。実キュー名には既存のname prefixを付ける。

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

イベント全体は`ArticleAssessedInScopeEvent`、payloadは既存`ArticleAssessedInScope`を正本とし、consumer追加のために送信形式を変更しない。SQSが付けるmessageIdとOutboxのevent_idは区別する。

```text
分析結果保存 + Outbox記録
    → relay Lambda
    → article-embedding SQS
    → LambdaのSQS受信機構
    → EmbeddingConsumer
    → Ready構築 → AI呼び出し → ベクトル・成功監査の保存
```

consumer Lambdaは既存Taskiq workerへ依頼を中継せず、自身で業務処理を実行する。既存のReady・AI adapter・Service・Repositoryを再利用し、TaskiqのContext、retry label、brokerの起動処理をSQS入口へ持ち込まない。

### 送受信で共有する入力検証

発行元のpayload検証・Outbox保存を維持し、relayのSQS publisherで保存済みの5項目を`ArticleAssessedInScopeEvent`として検証する。共通型はUUID・タイムゾーン付き日時・対象イベント種別・整数バージョン1・型付きpayloadを保証し、未知項目や数値文字列・真偽値・小数から整数への変換を拒否する。UUIDと日時のJSON文字列は明示的に復元する。記事存在やID同士の対応のDB照合は行わない。

受信本文は`app/lambda_handlers/embedding/event.py`の`parse_assessed_in_scope_event(message_body: str)`で解析し、同じ共通型を返す。後続のハンドラーはevent_id・occurred_atを追跡情報として保持し、payloadだけをConsumerへ渡す。JSONの重複キーとNaN・Infinityを拒否する。

不正本文は`EmbeddingEventInvalidError`で伝える。理由はJSON解析の`invalid_json`、外側の構造・項目型の`invalid_envelope`、対象外種別の`unsupported_event_type`、未対応版の`unsupported_schema_version`、payload内部の`invalid_payload`の順で優先する。payload自体の欠落や非オブジェクトは外側の構造不正に含む。共有のassessed_event_validation_failureは、大分類と重複のない不変の検証詳細を返す。詳細は既知の項目名と固定コードだけとし、未知キーはeventまたはpayloadのunknown_fieldへ置き換える。本文・入力値・検証自由文を属性や原因・contextに保持せず、本文解析関数内ではログ・監査・通知を行わない。JSON解析失敗はinvalid_jsonと空の詳細一覧で返す。

送信前の契約違反は既存の`PublishEventInvalidError`と個別の`PublishFailed`へ変換し、不正イベントだけをOutboxの自動配信停止へ進める。正常な同一バッチのイベントは送信する。これは受信後のSQS再配信やDLQ移動とは別の処理である。既存のpublisher呼び出し契約違反・送信先判定は維持し、日時不正も共有契約のinvalid_envelopeとして扱う。

実装済みは共通型・送信前検証・本文解析と、次節のSQSメッセージ処理まで。依存を組み立てるLambda起動関数とイベントソースマッピングは後続とする。

### SQSメッセージ処理と部分バッチ応答

`app/lambda_handlers/embedding/handler.py`の`_run_embedding(lambda_event, settings)`は、資源とConsumerの組み立て、バッチ検証、レコードの逐次処理、失敗IDの集約、資源終了までを進める。ループ内で本文の取り出し、parse_assessed_in_scope_eventによる業務イベント検証、consumer.consumeの実行と結果記録を順に行う。SQSへの直接操作は行わない。

最初に入力がオブジェクト、Recordsが配列、各レコードがオブジェクトであることを確認する。全messageIdの存在・文字列型・空白だけでないこと・重複がないことをConsumer実行前に確定する。構造不正はSqsInputErrorとして呼び出し全体へ伝え、ログには固定の項目名・理由・0始まりのレコード位置だけを記録する。不正なIDそのものは記録しない。

有効なmessageIdは加工せず保持し、入力順に1件ずつbodyを検証してConsumerへpayloadを渡す。使用しないSQSフィールドは許容する。bodyの欠落・非文字列・本文不正・Consumerの通常例外は個別失敗として後続処理を続ける。ConsumerはEmbeddingCompletionのSAVED・ALREADY_EMBEDDED、またはReady側のEmbeddingReadyBuildRejectedを返し、ハンドラーはその契約に従って結果を記録する。キャンセル・プロセス終了は伝播する。

_run_embeddingは失敗したメッセージのIDをSqsBatchItemIdentifierに格納し、入力順のlist[SqsBatchItemIdentifier]として返す。公開handlerはそのfailed_itemsをSqsBatchFailureResponseのbatchItemFailuresに含める。全件成功・空のRecordsは`{"batchItemFailures": []}`、失敗時は`{"batchItemFailures": [{"itemIdentifier": "失敗したmessageId"}]}`とする。複数件という理由では拒否せず、内部並列処理は追加しない。

ログは次の固定イベントを用い、通常のログ障害で結果や後続処理を変更しない。

| ログイベント | 記録内容 |
|---|---|
| embedding_sqs_input_invalid | reason、field、record_index。個別応答を作れない構造不正 |
| embedding_message_input_invalid | message_id、reason、issues。body自体の欠落・型不正はinvalid_bodyとbodyの項目コード |
| embedding_message_completed | message_id、検証済みevent_id、analyzed_article_id、正常完了reason |
| embedding_message_failed | message_id、例外の完全修飾型名。本文検証済みならevent_id・analyzed_article_idも付与 |

本文・自由文・トレースバックはログに渡さない。入力不正は構造化ログのみとしDB監査は追加しない。Consumerの監査・計測・枯渇通知は入口で重複実行しない。

初期の受信件数は後続のSQSトリガー設定で1件とし、ReportBatchItemFailuresを有効化する。Consumerの60秒制限は維持し、本番の受信件数を増やす際はLambda全体の時間配分を別途検討する。今回の実装ではLambda関数・Terraform・デプロイを変更しない。

## 成功・失敗の契約

| 結果 | consumerの扱い | メッセージの扱い |
|---|---|---|
| ベクトルと成功監査のコミット完了 | 成功 | 対応完了として削除対象 |
| 開始時点ですでに生成済み | 想定内の終了 | 対応完了として削除対象 |
| 他の実行が先に保存し、自分の更新が不要 | 生成済みを確認して想定内の終了 | 対応完了として削除対象 |
| 開始時に対象記事が存在しない、またはReady入力制約違反 | Ready構築拒否の理由を記録 | 受信完了、失敗一覧へ含めない |
| AI処理中に対象記事が削除され、保存できない | 対象記事不存在の失敗を記録 | SQSへ失敗を返す |
| 入力不正・未対応のイベント、実APIの拒否・429・通信障害・5xx | 失敗を記録 | SQSへ失敗を返す |
| 利用枠枯渇・残高不足・設定不備 | 失敗を記録し、既存の該当する通知を維持 | SQSへ失敗を返す |
| DB処理・コミットの失敗、処理時間上限到達、想定外例外 | 失敗として扱う | SQSへ失敗を返す。強制終了時も成功応答しない |

SQSによるメッセージ削除はLambda連携の成功処理に任せる。consumerから個別の削除APIは呼ばない。

失敗監査自体がDB障害で保存できない場合も成功にはしない。Lambdaの強制終了ではアプリケーションの失敗記録を実行できない可能性があるため、Lambda側の失敗観測も必要とする。

### 保存時の状態確認

共通の`EmbeddingService`はAI処理を終えた後に保存用トランザクションを開始し、記事IDで`SELECT ... FOR UPDATE`して保存対象をロックする。Repositoryの`lock_save_state()`は、存在と生成状態を`EmbeddingSaveState`の3状態として返す。

- `ARTICLE_MISSING`：`EmbeddingAnalyzedArticleMissingError`を送出する。
- `EMBEDDED`：`EmbeddingCompletion.ALREADY_EMBEDDED`を返し、成功監査は重複させない。
- `UNEMBEDDED`：条件付きUPDATEと成功監査を同一トランザクションでコミットした後に`EmbeddingCompletion.SAVED`を返す。

行ロックはAI待機中には保持せず、保存時からトランザクション終了まで保持する。記事の削除が先に確定した場合は不存在となり、保存側が先にロックした場合は削除が待機する。ロック取得・更新・コミットの失敗は呼び出し元へ伝播する。ロックした未生成行の更新が0件になる矛盾も正常終了させない。

`EmbeddingSaveState`は保存前のDB状態、`EmbeddingCompletion`はServiceの正常終了結果を表す。記事不存在・API障害・DB障害は結果値に変換せず例外で伝える。`EmbeddingCompletion`自体をSAVED・ALREADY_EMBEDDEDの2種類のStrEnumとし、失敗を表す値は含めない。

Serviceの失敗は`EmbeddingError.reason`（`EmbeddingFailureReason`）で表し、再試行方針を持たない。

| reason | 詳細 | 既存Taskiqでの対応 |
|---|---|---|
| `ARTICLE_MISSING` | 保存対象の記事が存在しない | `embedding_analyzed_article_missing`／`target_missing`、追加再試行なし |
| `RESPONSE_INVALID` | 埋め込み応答がベクトルの契約を満たさない | `embedding_response_invalid`／`ai_response_invalid`、既存上限まで再試行 |
| `PROVIDER_ERROR` | 元のprovider例外に429・通信障害・残高不足などの分類と詳細を保持 | providerの既存FAILURE_MODEから再試行・holdを判断 |

DB障害と想定外例外は`EmbeddingError`に包まず伝播する。`code`はreasonと元のprovider例外から導出し、監査コードを二重管理しない。

`task_errors.py`の変換を既存Taskiq境界で行い、Serviceの例外を`EmbeddingRecoverableError`／`EmbeddingTerminalError`に対応付ける。既存の監査コード・failure_kind・failure_reason・retryability・通知providerは維持する。互換例外のモジュール移動に伴い、新しく記録するerror_classの完全修飾名は変わり、error_chainにはService例外が加わる。過去の監査データは変更しない。

既存Taskiqは正常終了結果を受け取って完了し、再試行・監査・通知の扱いは維持する。

記事不存在の場合は対象行のロックを取得せず、例外でセッションを終了する。生成済みの場合はその場でreturnし、セッション終了時のロールバックで行ロックを解放する。

保存時の記事不存在は`embedding_analyzed_article_missing`／`target_missing`として既存Taskiqの失敗handlerでFAILED監査に記録する。Taskiqでは既存のTerminal処理に従い追加再試行・holdを行わない。開始時のReady構築におけるTaskiqの既存REJECTED処理は今回変更しない。

consumerでは両者を区別し、生成済みを確認できた場合だけ対応完了とする。単なる更新0件、失敗handlerによる例外抑止、試行上限到達を成功の根拠にしない。状態確認自体の失敗もSQSへ失敗を返す。

### Consumer本体の処理

`app/analysis/embedding/consumer.py`の`EmbeddingConsumer(session_factory, embedder)`が、検証済みの`ArticleAssessedInScope`を`consume(event)`で受け取る。TaskiqのContextやLambda/SQS形式には依存しない。

1. `analyzed_article_id`で開始状態を一度取得し、取得できた元記事IDを保持する。イベントの`curation_id`との対応は発行元の保存・Outbox生成契約を信頼し、再照合しない。
2. 取得用セッションを閉じ、`ReadyForEmbedding.from_facts()`で純粋に開始条件と入力を検証する。Taskiqの既存`try_advance_from()`もこの関数へ委譲し、成功時のtupleとhintの優先順位を維持する。拒否は例外にせず、Ready側の不変な`EmbeddingReadyBuildRejected`で返す。
3. 開始時の不存在・入力不正では、同じ拒否値を監査へ渡して受信完了結果として返す。生成済みの拒否理由ならAI・成功監査・成功計測を行わず`ALREADY_EMBEDDED`で完了する。
4. 未生成ならServiceを実行して`EmbeddingCompletion`をそのまま返す。保存時の競合・削除・コミット失敗の契約を維持する。
5. 開始時からService完了までの通常の例外は、分類・後処理を経て再送出する。分類や後処理自体の予期しない二次障害でも元の例外を維持し、安全なログを試みる。外部キャンセルは通常の失敗として処理しない。

元記事IDを取得できなかった場合は`article_id=NULL`として分析記事IDをpayloadに残す。開始時の不存在は`embedding_ready_build_blocked_analyzed_article_missing`のREJECTED監査とする。Ready入力不正はDB由来記事IDを使用し、イベントやhintで補完しない。実行失敗では取得済みの記事IDを使用し、監査不能でも既存のdrop計測・通知を試みて元の例外を伝播する。

今回は既存Taskiqの入口や配置を移動せず、Consumer専用トレースの配線・Lambda起動関数・デプロイは後続に残す。

### Ready拒否の受信完了（2026-09-13）

実装・検証済み。全単体6,655件・全DB統合1,402件が成功し、拒否監査の後処理側への移動後にも関連単体745件・両工程のDB統合219件を再確認した。Ruffも成功し、AWS適用は行っていない。

本節は開始時の不存在・Ready入力検証に関する過去のスライス記録を更新する。

- `domain/ready.py`の`EmbeddingReadyBuildRejected`が`EmbeddingReadyBuildRejectionReason`と任意のDB由来記事IDを持つ。`EmbeddingReadyBuildBlockedError`を廃止し、Ready・Consumer・監査・旧Taskiqで同じ値を用いる。
- Readyモデル生成時の入力検証エラーだけを`INPUT_INVALID`へ対応付ける。テキスト生成ルール・入力制約を維持し、DB取得障害や想定外例外を拒否へ変換しない。
- Consumerの戻り値は`EmbeddingCompletion | EmbeddingReadyBuildRejected`とする。ServiceのCompletionは変更せず、欠損・入力不正ではService・AI・成功監査・後続Outbox・成功／実行失敗メトリクスを呼ばない。記事は保持する。
- `append_ready_build_rejected`へReady側の値を渡し、`REJECTED`と理由コードを記録する。本文・入力値・検証例外を保存せず、既存の`embedding_ready_build_blocked_*`文字列は維持する。通常の監査障害は安全なログとaudit-dropped計測へ退避し、受信完了を維持する。
- 拒否監査は業務処理の60秒制限を抜けた後に、`EmbeddingConsumerFailureHandler.handle_ready_build_rejected`が行う。実行失敗の分類・計測・通知は通さない。
- Lambdaは拒否のmessageIdを`batchItemFailures`へ含めず、`reason=ready_build_rejected`と`rejection_code`を記録する。SQS削除APIは呼ばない。Ready成立後の実行失敗とキャンセルの契約は維持する。
- DB schema・イベントpayload・資源ライフサイクルを変更しない。旧Taskiqの共有Ready呼び出し元は値による分岐へ更新し、救済を存続させる。

### Consumerの失敗分類と後処理

分類関数とConsumer用ハンドラーを実装し、Consumer本体から開始時の取得・Ready構築・Service実行中の失敗を接続する。入力イベントの検証・SQS応答は入口部品へ接続し、Lambda起動関数の組み立ては後続とする。

- `classify_embedding_failure(exc)`は副作用のない関数で、`EmbeddingFailureClassification`を返す。監査用の`FailureProjection`、監視上の`failed`／`infra_error`、必要な枯渇通知の元例外を持つ。
- Serviceのreasonと元のprovider例外から直接分類し、Taskiq用のRecoverable／Terminalには変換しない。DB例外は共有のDB分類を使う。想定外例外と通常のTimeoutErrorは`unexpected_error`／`unknown`として失敗に分類する。
- 監査上のretryabilityは失敗の性質として維持するが、例外抑止・再配信・holdの判断には使用しない。
- `EmbeddingConsumerFailureHandler.handle()`は分類結果、元の例外、分析記事ID、記事ID、providerを受け取り、失敗件数の計測・失敗監査・必要な枯渇通知をそれぞれ試みる。戻り値はNoneで、処理全体の成功を示す値ではない。
- 監査は元の例外のerror_class・error_chainを保持し、既存のpayload組み立てと秘匿処理を再利用する。枯渇通知は既存の`ai_provider_exhausted`打点で、残高不足・利用枠枯渇のみを対象とする。通常の429は枯渇通知の対象外。
- 後処理の通常の例外は捕捉し、処理名・記事ID・例外クラスだけを二次障害ログに記録する。例外本文やトレースバックは出さない。監査失敗時は既存の監査drop計測も試みる。ログ出力自体の失敗でも残りの後処理を継続する。
- 元の例外の再送出は後続のConsumerの責務であり、ハンドラー内では再送出も成功への変換も行わない。キャンセルやプロセス終了を通常の二次障害として抑止しない。

既存Taskiqのハンドラーと起動配線は維持する。枯渇通知の対象判定だけを共有の純粋関数へ切り出し、既存通知の条件と出力を維持する。

## 実行・再配信設定

| 項目 | 合意値 |
|---|---|
| SQSの種類 | 既存Standardキュー |
| 1起動で処理するメッセージ数（BatchSize） | 1 |
| バッチ待機時間（MaximumBatchingWindowInSeconds） | 0秒 |
| SQSトリガーの最大同時実行数（MaximumConcurrency） | 10 |
| consumer Lambdaの予約済み同時実行数 | 10 |
| 業務処理の時間上限 | 60秒 |
| Lambda全体のタイムアウト | 120秒 |
| SQS可視性タイムアウト | 720秒（12分） |
| DLQへの受信回数上限（maxReceiveCount） | 5 |
| embedding元キューの保持期間 | 4日 |
| DLQの保持期間 | 14日 |

1起動1件であり、関数内で複数記事を並列処理しない。必要に応じて最大10起動が並行する上限で、常時10起動する設定ではない。

業務処理の60秒はReady構築・AI呼び出し・保存を対象とし、Lambda全体の120秒との差は初期化・失敗記録・接続終了の余裕とする。Consumerの`asyncio.timeout(60)`は開始状態の取得からService完了までを対象とし、失敗後処理はその外側で実行する。これは協調的キャンセルによる上限であり、Lambdaの強制終了やSDK設定は後続で扱う。

可視性タイムアウト720秒は、Lambda timeoutの6倍とするAWS推奨に従う。受信時点からの不可視期間であり、失敗時点から12分後の実行予約ではない。期限後に再受信可能となるが、再実行時刻を保証しない。Standardキューの重複配信は引き続き許容する。

maxReceiveCountはSQSの受信回数上限であり、アプリケーションの正確な実行回数を保証する値ではない。SQSのredrive policyでDLQへ移動させ、consumer自身は直接移動しない。DLQからの自動再投入は初期実装に含めない。

### 保持期間と接続経路

- embedding元キューのみ14日から4日に変更し、他工程は維持する。適用前に4日より古いメッセージの滞留を確認する。
- StandardキューはDLQ移動後も元の投入時刻を保持期限の基準とする。元キューの期限切れはDLQ移動ではなく削除となる。
- AI通信は既存の外向きプロキシ経由でGeminiへ接続する。Consumerの送信元と必要な宛先に対応する許可を設ける。
- 記事取得・結果保存はRDSへIAM認証・TLSで接続する。秘密情報はSSMのVPCエンドポイント経由で取得する。
- AI APIキーは既存と同じ値を `/<prefix>/embedding-consumer/gemini-api-key` にSecureStringとして登録し、Consumerの読取権限を専用パスに限定する。APIキーの発行単位は変更しない。
- SSMパラメーターの作成・値の登録は既存の初回構築手順に従いTerraform管理外とする。Lambda向けの取得・初期化処理は別途実装する。

### Lambda呼び出し単位の接続・APIキー管理（合意済み）

Problem: SQS処理部品へ依存を渡すLambda起動部分について、接続・秘密情報の寿命と設定の責務を明確にする。
Evidence: 実装済みのレコード処理とConsumerの依存注入契約、既存のDB Engine・Gemini SDK利用箇所、および接続再利用・終了に関する公式資料を参照する。既存relayやGeminiEmbedderの構築方法を、そのまま新しい入口の設計条件にはしない。

- 共有範囲は1回のLambda呼び出しとする。ハンドラー側の組み立て処理がDB Engine・AIクライアントを準備し、その呼び出しに含まれるメッセージで共有して、処理と失敗後処理が終了したら閉じる。次の呼び出しへは持ち越さない。
- 現在はBatchSize=1のため1件ごとに準備・終了する。将来複数件を受信する場合も、同じ呼び出し内で逐次処理する間だけ共有する。受信件数や全体の時間配分は、その変更時に別途検討する。
- Engineの共有と、セッション・トランザクションの共有は区別する。各記事の保存は独立して確定し、取得・保存・失敗記録のセッション境界を維持する。AI応答待ちの間にDBセッションを保持しない。
- クライアント生成時の事前接続確認は追加しない。DBは必要なSQLの実行時、AIは生成が必要になった時点で通信する。Engineは1接続・追加接続0のプールで、同じ呼び出し内の物理接続を再利用する。
- APIキーは1回のLambda呼び出しにつきSSMから1回取得し、その呼び出し内の全メッセージで共有する。呼び出しをまたぐキャッシュ、TTL、キャッシュ用Extensionは追加しない。SSM SDKの短い再試行は、この1回の取得操作に含める。
- 設定にはSSMパスを保持し、取得したAPIキーの実値はクライアント生成時に渡す。値を環境変数へ書き戻したり、ログ・仕様・Terraform stateへ出したりしない。
- 毎回取得する目的は、呼び出し間のキャッシュ更新管理を不要にすること。SSMの通信時間・一時障害・スロットリングの影響は各呼び出しで受ける。同時実行数10を毎秒のSSM要求数上限とは扱わない。

| 担当 | 責務 |
|---|---|
| 接続設定 | 接続先・プロキシ・通信タイムアウト・SDK内部再試行を宣言的に表す。秘密情報の実値は持たない |
| クライアント生成・終了 | 接続設定と取得した秘密情報からクライアントを生成し、所有する資源を閉じる |
| Gemini Embedder | 渡されたクライアントでEmbeddingを要求し、応答とAPI例外を既存の業務契約へ変換する |
| Lambda起動部分 | 取得・生成・Consumer組み立て・SQS処理部品の呼び出し・終了の順序を管理する |

接続設定は設定層経由で扱う。モデル名・ベクトル次元・task typeなどのEmbedding仕様は通信設定に混ぜない。Gemini・SSM・DBに必要な具体的な設定と生成処理から始め、汎用の接続登録機構・新しい依存パッケージは追加しない。

初期化でSSM取得やクライアント準備に失敗した場合はConsumerを呼ばず、Lambda呼び出し全体を失敗にする。部分的に準備できた資源も終了処理の対象とする。既存のメッセージ単位の監査・通知を初期化失敗へ流用しない。

終了処理の通常例外は安全なログを試み、確定済みの部分バッチ応答や先行例外を置き換えない。一つの終了処理が失敗しても、他の生成済み資源の終了を試みる。本文・秘密情報・例外自由文はログに出さない。キャンセル・プロセス終了を通常の終了失敗として抑止しない。

Invariants: Consumerの業務処理60秒・失敗後処理の境界、部分バッチ応答、成功監査の一回性とTaskiq併用を維持する。
Non-goals: relayの再設計、呼び出し間の資源共有、受信件数変更、内部並列化、新しいキャッシュ・再試行機構、AWS適用はこの方針整理に含めない。
Done: 合意事項と未確定の数値・実装詳細を区別し、次節の各スライスで実装・検証する条件が明確であること。方針合意を実装完了とは扱わない。

### 通信設定（スライス3.2まで確定）

Geminiはスライス3.1、SSMとRDSはスライス3.2で下記の設定を確定した。これらは通信・コマンド単位の上限であり、呼び出し全体の経過時間保証ではない。

| 接続 | タイムアウト | SDK内部再試行 | 確定したスライス |
|---|---|---|---|
| Gemini（既存プロキシ経由） | 接続3秒・応答読み取り待ち10秒・書き込み待ち10秒・プール待ち3秒 | SDKは初回のみ、HTTP transportも再試行なし | 3.1（確定） |
| SSM（専用に許可されたVPCエンドポイント経由） | 接続3秒・読み取り5秒 | 初回を含め最大2回 | 3.2（確定） |
| RDS（IAM認証・TLS） | 接続5秒・SQLコマンド5秒・プール取得5秒、1接続・追加接続0 | 独自の再試行なし | 3.2（確定） |

読み取りタイムアウトは通信中の待ち時間を制限するもので、処理全体の経過時間上限とは区別する。Consumerの60秒を維持し、3.3では新しい全体タイマーを追加しない。初期化・失敗後処理・終了処理を含めたLambda全体120秒の設定は3.4で行う。

参考: [AWS Lambdaの接続再利用](https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html)、[SSM取得とキャッシュ](https://docs.aws.amazon.com/systems-manager/latest/userguide/ps-integration-lambda-extensions.html)、[SQLAlchemyのイベントループ間共有制約](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops)、[Google Gen AI SDKのクライアント終了](https://googleapis.github.io/python-genai/#close-a-client)、[HTTPXのタイムアウト](https://www.python-httpx.org/advanced/timeouts/)、[Botocoreの設定](https://docs.aws.amazon.com/botocore/latest/reference/config.html)。

### 送信側設定との区別

relayの120秒timeout、Outboxの150秒lease、最大5回の送信試行、30・120・600・1,800秒を基準とする再送待ちは送信側の契約であり、変更しない。

受信側にOutboxのleaseや送信側バックオフを転用しない。relayが最大10件を一括送信しても、consumerは1件ずつ受信する。

## Taskiqとの併用

- Taskiqによる分析直後のembedding投入と、backfillによる再投入を残したままconsumerを有効化する期間を許容する。
- Taskiq・Lambdaとも同じ分析記事の生成済み判定・条件付き保存に従い、二重保存・成功監査の重複を防ぐ。
- 同時に未生成と判断した場合のAI呼び出し重複は許容する。AI呼び出しを厳密に1回にするための新規ロック・処理済みeventテーブルは追加しない。
- consumerが失敗した後にTaskiqが保存を完了した場合、次のSQS受信では生成済みとして対応完了にできる。
- embedding workerが1プロセスの通常構成では、Taskiq最大10とLambda最大10で合計最大20記事が並行し得る。workerの複数配置・デプロイ時の新旧併存では増え得る。
- AI待機中にDBセッションを保持しない既存の構造を維持する。並列記事数とDB接続数を同一視せず、他の工程も含むDB負荷を有効化時に確認する。
- 処理時間・DB接続数・429を既存の監視で確認し、負荷が高い場合はSQSトリガーの同時実行上限を下げる。

## Non-goals

本文補完・本文整形・投資判定consumerの実装、Taskiqの即時撤去、backfill予算の変更、無料枠ゲートの再導入、送信契約の変更、DLQへの直接送信、DLQからの自動再投入、AI呼び出しのexactly-once保証は含めない。

スライス1はTerraform・テスト・仕様・適用手順の変更までとし、AWS適用・秘密情報登録・Schedulerやconsumerの有効化を行わない。

## 実装順序とDone

1. DLQ、元キューの再配信・保持設定、Consumer専用の権限・ネットワーク・ログを整備する。この段階では受信を開始しない。
2. consumer本体を実装し、成功・失敗・競合・記事不存在の契約をローカルで検証する。
3. Lambda handler・実行イメージ・関数と無効状態のSQS起動トリガーを整備する。
4. 配置・SSM設定を確認してConsumerの受信を先に有効化する。次にスライス4.2の方針でrelayの送信を開始し、実データで通信・実行・再配信・DLQ移動・Taskiqとの併用を確認する。

Doneは、正常処理・生成済み・競合でメッセージが対応完了となり、失敗が記録され、再配信上限後にDLQへ移り、Taskiqとの重複でDB結果を壊さないことを検証できた状態とする。ローカル実装完了と、AWS上での有効化・検証完了は区別して記録する。

### スライス3の分割：Lambda起動部分から受信前の構築まで

各スライスは実装前に残る判断を確定し、この仕様へ反映してから進める。3.1〜3.3はローカルの実装・検証まで、3.4はコード・Terraform・適用手順までとし、AWSへの適用・秘密情報登録・受信有効化は別途扱う。

| スライス | 実装範囲 | 実装前に決めること | 完了条件 |
|---|---|---|---|
| 3.1 Gemini共通通信設定とクライアント管理 | app/ai_providers/geminiで通信設定と生成・終了を実装 | 接続3秒・読み取り10秒・書き込み10秒・プール待ち3秒、SDK再試行なしで確定 | 実SDKとモック通信で実効timeout・単一試行・資源解放を確認。既存Embedder・Taskiqは変更しない |
| 3.1b 新しいEmbedderへの接続 | 共通クライアントを受け取る新しいEmbedderを実装 | 配置・命名、既存の業務仕様と例外翻訳の再利用方法 | モデル・次元・入力・応答・例外の契約を維持し、既存Taskiqを変更せずConsumerに渡せる |
| 3.2 SSM取得とDB接続の組み立て | 呼び出し単位のAPIキー取得、専用設定からのDB Engine・セッション生成 | SSMのtimeout・試行回数、DBのtimeout・プール方式・application_name、設定型と配置 | 呼び出し間でキー・接続を共有せず、部分的な初期化失敗でも資源を閉じる。SSMはモック、DBのセッション・トランザクション境界は実DBで検証 |
| 3.3 Lambdaハンドラーへの接続 | 設定・取得・生成・Consumer組み立て、バッチ検証と逐次処理、終了と応答 | 同期handlerからasyncio.run、初期化後に入力検証、空Recordsも初期化、新規全体タイマーなし、初期化ログは工程・例外型だけで確定 | 同一呼び出し内での共有と別呼び出しでの再生成、成功・個別失敗・初期化失敗・終了失敗・キャンセルの契約を検証。終了障害が確定済み応答を変更しない |
| 3.4 実行イメージと無効状態のトリガー | Lambda用イメージ、関数、SQSイベントソースマッピング、専用基盤の配線、CI権限・適用手順 | メモリ1024MB、共通arm64イメージの独立digest指定、専用CI権限・既存ログで確定 | BatchSize=1・最大同時実行10・予約済み同時実行10・timeout120秒・ReportBatchItemFailuresを設定し、トリガーは無効。構築・設定を検証して適用手順を揃える |

3.1〜3.3のbackend変更は`/check`に従い、Ruff lint・format、全単体テスト、`make test-integration`を順に行う。外部AI・AWSをモックし、既存TaskiqとConsumerの契約を保つ。3.4は変更範囲に応じたイメージ・Terraform検証を追加する。実通信・通知配送・再配信・DLQ移動と手動停止・再開・再投入は既存スライス4で実証する。

方針・スライス整理（2026-09-10）: 文書のみを更新。差分と既存の合意値・境界の整合性を確認し、コード変更がないためテストは実行しない。この記録時点では各スライスは未着手で、以降の実装状況は下記Implementationに記録する。

## Implementation

### スライス4.1：ConsumerのSQS受信有効化

Problem: 配置済みConsumerの受信を有効化し、relayの送信開始に先立って監視・停止・再開手順を整える。
Evidence: 本体apply #43の成功、LambdaのActive・1024MB・120秒、マッピングのDisabled・BatchSize=1・最大同時実行10・ReportBatchItemFailures、専用SSMのSecureString・標準・alias/aws/ssmを読み取り確認した（2026-09-10）。同確認時の元キュー・DLQは各0件、relayのSchedulerはDISABLED。
Invariants: 有効化以外の実行設定、既存digest、キュー・DLQ、Taskiqの並行稼働を維持する。relayはDISABLEDのままとし、秘密値を取得・記録しない。
Non-goals: 今回はコード・テスト・仕様・手順まで。AWS適用、relay有効化、イメージ公開、bootstrap・IAM・ネットワーク・アプリ変更、新しい設定スイッチ・ignore_changes・自動停止・独自再試行は含めない。
Done: 同じマッピングをenabled=trueへ更新する設定とmockテスト、適用・監視・停止・再開手順が一致する。実CI planと適用後の状態確認、実データ処理の確認はそれぞれの実施時点で結果を記録する。

現在の目標状態はConsumerの`enabled=true`であり、将来の新規作成時もdigestを指定すると有効なマッピングを作成する。初回digest未指定なら従来どおり関数・マッピングは作成しない。以下のスライス3.4に記載した無効配置は過去の構築段階の記録として保持する。

適用は既存production承認付きworkflowで行い、relayとConsumer両方のdigestをstateから保持する。PR planの想定差分は既存マッピングの`enabled: false → true`だけで、再作成・削除・relay変更・Lambdaの環境変数や起動設定の不要差分があれば適用前に確認する。適用後はEnabledと設定値、relayのDISABLED、再planに不要な差分がないことを確認する。

CloudWatchの初期化・個別失敗ログ、既存計測、処理時間、スロットリング、キュー滞留、DLQ、DB負荷を確認する。部分バッチ応答の失敗はLambdaのErrorsだけでは判断しない。relay停止中でキューが空の場合の未実行を異常扱いせず、受信設定の有効化を実処理の成功と同一視しない。

共通の接続・認証障害が繰り返される場合や継続する設定不備が判明した場合は、管理者CLIで対象関数・元キュー・一意のUUIDを確認して受信を手動停止する。個別記事の失敗は既存の再配信・DLQへ任せる。停止中の再有効化applyは承認せず、Terraformもenabled=falseへ反映する。原因解消後はTerraformからenabled=trueへ戻し、承認付きapplyで再開する。CLI停止は実行中処理を強制終了せず、キューのメッセージを削除しない。

具体的なコマンドと確認項目は[受信有効化・監視・停止・再開手順](../../infra/aws/README.md#consumerの受信有効化監視停止再開スライス41)を参照する。この時点の次工程はrelayの定期送信有効化とした。開始前の件数確認を省略する後続の決定はスライス4.2を参照する。実通信・再配信・DLQ移動・Taskiqとの実併用は送信開始後に確認する。

検証結果（2026-09-10）:

- 変更Terraform設定・テストのfmt -check、本体のbackend未接続init・validate・全mock 7件が成功した。既存Service Discoveryのfailure_threshold非推奨警告は残る。
- READMEの3つのコマンドブロックをbash -nで確認した。停止手順はAWSを置き換えた13シナリオで、正常停止・状態遷移待ち・対象なし/複数・関数/キュー不一致・不正UUID/アカウント・取得/更新失敗・異常状態・待機上限を確認し、対象不正では更新を呼ばないことを検証した。実AWSでの停止操作は行っていない。
- backend/frontendコードとイメージは未変更のため、全アプリテスト・イメージビルド・bootstrap検証は再実行していない。
- vector-planはGetRoleCredentialsのForbiddenException（No access）で認証できず、ローカルの実環境planは未実施。実CIのPR planはPR作成後に確認し、想定差分以外があれば適用しない。AWSへの有効化apply、適用後の状態・再plan、実データ処理の確認は未実施。

### スライス4.2：relayの定期送信有効化

Problem: 更新済みrelayの定期送信を開始できるようにし、開始後にSQS送信とConsumerの実処理を確認する。
Evidence: 2026-09-10に本体apply #44によるConsumer受信有効化と、apply #45によるrelayイメージ更新が完了した。relayはActive・LastUpdateStatus=Successful、SchedulerはDISABLED。配置したイメージのhandlerはOutboxRelay.run_onceを実行し、article.assessed_in_scopeだけをembeddingキューへ送信する。
Invariants: 既存SchedulerだけをENABLEDへ更新する。1分間隔・Flexible Time Windowなし、relayの512MB・120秒・予約同時実行1、1回最大10件の確保、Consumerの有効な受信・実行上限、独立したdigest、他工程・Taskiqを維持する。
Non-goals: この変更ではAWS適用・手動invoke・イメージ公開・digest更新・秘密値取得・bootstrapやアプリの変更を行わない。件数確認用のDB接続基盤、新しい制御・自動停止・再試行は追加しない。
Done: 設定・mockテスト・適用後の監視と停止手順が一致する。実CI plan、production承認付きapply、送信・保存の実証は実施時点で別に記録する。

ユーザーの選択により、開始前のOutbox未配信件数の確認は省略し、件数不明のまま開始後の実処理を確認する。従来の「未配信件数を先に確認する」順序をこの方針で置き換える。発生日時による除外はなく、古い未配信イベントも対象になる。対象は既存の未配信・未停止・再試行時刻到来・lease期限・試行上限条件を満たすarticle.assessed_in_scopeであり、他工程のイベントは送信しない。

現在の目標状態はSchedulerのENABLEDであり、将来の新規作成でも有効になる。digest未指定で未作成の場合は従来どおり関数とSchedulerを作成しない。通常workflowではrelay・Consumerの既存digestを維持する。想定planは既存Schedulerのstate: DISABLED → ENABLEDの1更新だけで、Lambda・キューの再作成や不要な設定差分があれば適用前に確認する。

開始後はrelayの起動・送信停止ログ、SQS送受信、Consumerの完了・失敗ログと成功監査・保存結果、DLQ、処理時間・スロットリング・DB負荷を確認する。1回の確保上限10件は、再試行や重複起動を含む1分間の厳密な送信上限ではない。キューが空であることやLambdaの正常終了だけでは保存成功を証明しない。再配信・DLQ移動・Taskiq併用は観測できた範囲を記録し、未確認を成功扱いしない。

共通障害が継続したらSchedulerを手動停止し、必要ならConsumer受信も既存手順で停止する。TerraformもDISABLEDへ戻し、原因解消後に承認付きapplyで再開する。詳細は[relayの運用手順](../../infra/aws/OUTBOX_RELAY.md#定期送信の開始と監視スライス42)を参照する。

検証結果（2026-09-10）:

- 変更したTerraform設定・テストのfmt -check、本体のbackend未接続init・validate・全mock 7件が成功した。SchedulerのENABLED・起動間隔・対象関数、Consumerの有効な受信と各上限、独立digest、初回未指定時の未作成を確認した。既存Service Discoveryのfailure_threshold非推奨警告は残る。
- git diff --checkが成功した。実行設定の差分はSchedulerのstateだけで、bootstrap・IAM・ネットワーク・workflow・イメージ・アプリコードは変更していない。backend/frontendの全テスト、イメージビルド、未変更bootstrapの検証は対象外として再実行していない。
- vector-planは先行調査でGetRoleCredentialsのForbiddenException（No access）となっており、ローカル実環境planは未実施。実CIのPR planはPR作成後に確認する。今回のAWS適用、適用後の再plan、定期送信・保存の実証と緊急停止操作は未実施。

### スライス3.4：Lambdaと無効状態のSQSトリガー

Problem: 完成したConsumerを共通backendイメージから配置し、独立した版管理と専用基盤へ接続する。
Evidence: 既存relayのdigest保持・承認付きTerraform経路、Consumerの専用SG・IAM・ログ、実装済みhandlerを基準にする。
Invariants: メモリ1024MB、timeout120秒、予約同時実行10、BatchSize=1、収集待ち0秒、最大同時実行10、ReportBatchItemFailures、enabled=false。プロキシ必須、既存のキュー・DLQ・Taskiq・relay・ECS更新経路を維持する。
Non-goals: AWS適用、イメージ公開、秘密情報登録、受信有効化、新しいアラーム・自動停止・X-Rayは行わない。
Done: 関数と無効トリガー、独立digestの指定・省略時保持、専用基盤と制限付きCI権限を検証し、初回・更新・切り戻し手順を揃える。

`embedding_consumer_image_digest`はnullまたはsha256 digestとする。初回の未指定時は関数とトリガーを作成しない。通常plan/applyでは専用スクリプトがstate上の現行版を引き継ぎ、明示指定時だけ作成・更新・切り戻しを行う。state取得・検証失敗は停止し、nullへのfallbackをしない。Terraform変数だけでは現行版は保持されない。

起動はarm64の共通backendイメージ内のawslambdaricから`app.lambda_handlers.embedding.handler`を呼ぶ。ENV、IAM認証DB URL、DB_IAM_AUTH、専用SSMパス、EGRESS_PROXY_URLだけを明示し、AWS_REGIONはLambda提供値を使う。専用ロググループにText形式で出力し、アプリJSONとEMFの構造を維持する。承認後のplan再実行・applyの順序は既存CIに従う。

スライス3.4の検証（2026-09-10）:

- 本体・bootstrapのbackend未接続initとvalidateが成功。本体mock 7件・bootstrap mock 4件が成功し、追跡対象のTerraform設定・mockテストのfmt -checkも成功した。ローカルの未変更terraform.tfvarsは整形対象外とした。既存Service Discoveryの非推奨警告は維持している。
- Ruff lint・format check、全単体6,351件、実DB統合1,351件が成功。既存DB権限テスト22件はAlembic適用済みpublic.watchlist_entriesが必要なためスキップ。単体にはConsumer digestとworkflowの回帰テスト24件を含み、既存relayの独立スクリプトテスト5件も成功した。
- Linux ARM64の共通イメージをローカルビルドし、network none・read-only root・書き込み可能な/tmpでRICから実handlerへ2回接続した。SSM・DB資源をモックし、実GeminiクライアントとConsumerの生成、空Recordsの部分バッチ応答、呼び出しごとの資源終了を確認した。イメージは公開していない。
- actionlint 1.7.12は変更前から存在するconcurrency.queueキーに未対応のため、その既知警告1件に限って除外した検査が成功した。queue: maxとproduction承認・直列適用を削除・変更していない。
- vector-planのSSOトークンが期限切れでrefreshできず、実環境の読み取り専用planは未実施。AWS上のタグ付きマッピング作成、実通信・ログ配送・再配信・DLQ移動も未検証。AWS適用・秘密情報登録・受信有効化は実施していない。

### スライス3.4の未解決事項：CIのKMS設定読戻し（2026-09-10）

Problem: CIによるLambda設定の読戻しを阻害する全面復号Denyを、確認済みの対象・経路だけに限定して解消する。
Evidence: 実キーのポリシー、CloudTrail、実CIロールの明示Deny、provider v6.62.0の設定取得エラー時の挙動。
Invariants: 自アカウントSSMとSecrets Managerの秘密値取得拒否、他CIロールの全面復号拒否、対象外キー・関数・直接復号の明示Denyを維持する。
Non-goals: AWS適用、信頼ポリシー変更、新規キー作成、秘密値取得、受信開始。
Done: Terraformの権限・適用順・回帰テスト・手順が一致する。実環境の解消判定はbootstrap適用後の実CIロールによる読戻しと再planまで保留する。

既存のkms:Decrypt全面Denyを残したままでは、digest保持だけでLambdaの設定管理は完了しない。以下の調査はAWSへの書き込み・関数起動・秘密値取得を行わず実施した。

- 管理者のGetFunctionとGetFunctionConfigurationは、既存vector-outbox-relayの環境変数とimage_configをエラーなく返した。値は表示・保存せず、項目の存在とエラーコードだけを確認した。Consumer関数は未作成。
- relayにカスタマー管理キーの指定はなく、alias/aws/lambdaはEnabledのAWS管理キーだった。キーの実ポリシーにはLambda経由の利用条件とaws:lambda:FunctionArnの暗号化コンテキスト条件がある。CloudTrailの管理者による設定読取時のDecryptでも、lambda.amazonaws.com経由とrelayの関数ARNコンテキストを確認した。
- 実際のplan/applyロールにはNoSecretValuesのkms:Decrypt全面Denyが残る。管理者によるsimulate-principal-policyで両ロールのrelay復号がexplicitDenyであることを確認した。
- 未適用の候補ポリシーをsimulate-custom-policyで検証した。対象AWS管理キー・対象リージョンのLambda経由・relay/Consumerの関数ARNをすべて満たす復号をallowed、別キー・別関数・直接KMS・SSM経由・関数ARNコンテキスト欠落をexplicitDenyと判定した。追加AllowがあってもSSM GetParameterとSecrets Manager GetSecretValueの明示Denyを維持できた。シミュレーションは指定したコンテキストでの評価であり、候補ポリシーでの実Lambda管理API成功を実証したものではない。
- vector-adminのSSOログインは成功した。vector-deployはSSOログイン後のGetRoleCredentialsでForbiddenException（No access）となり、vector-planも認証情報を取得できなかった。アカウント設定の一致とVectorDeployロールの存在は確認できたが、利用者への割り当ては未確認。これはLambdaのKMS拒否とは別の認証問題。

修正コードでは、plan/applyの秘密値取得拒否とLambda設定の復号境界を分離した。`lambda_config_readback.tf`は既存の`alias/aws/lambda`の実キーARNを参照し、対象リージョンのLambda経由かつrelay/Consumerの暗号化コンテキストが揃う場合だけ復号を許可する。別キー・別経路・別関数・条件欠落は独立した明示Denyで拒否する。限定ポリシーを取り付けてから従来の全面Denyを外す依存関係を設け、適用途中も復号制限を維持する。

他CIロールの全面復号拒否と、全CIロールのSSM/Secrets Manager値取得拒否は維持する。新規キーは作らず、既存キーがない環境ではデータソース取得を失敗させる。Lambda環境変数には秘密値を置かずSSMパスだけを置く制約を維持する。

bootstrap適用後の実CIロールでの読戻しと再planの不要差分解消まで、実環境の問題は未解決として扱う。IAMシミュレーションは実Lambda APIの成功を保証しない。適用・確認の順序は[bootstrap手順](../../infra/aws/bootstrap/README.md#lambda管理設定の読戻し権限)に記載する。今回、AWSのIAM・キー・関数・トリガー・Terraform stateは変更していない。

KMS修正の検証（2026-09-10）:

- 本体・bootstrap双方でbackend未接続のinit、validate、追跡対象と新規設定のfmt -checkが成功。本体mock 7件・bootstrap mock 6件が成功し、生成される権限範囲、全CIロールの秘密値取得Deny、他CIロールの全面復号Denyを確認した。既存Service Discoveryの非推奨警告は残る。
- Ruff lint・format checkと、関連スクリプトの27テストが成功。この修正はTerraform・mockテスト・手順に限定されるため、上記で成功済みのbackend全単体・実DB統合・ARM64イメージ検証は再実行していない。
- Terraform mock planが生成した実際のポリシーJSONをAWS IAM simulate-custom-policyへ渡した。追加Allowのある24ケースで、対象2関数はallowed、別キー・別関数・修飾付き関数ARN・別リージョン・直接KMS・SSM経由・コンテキスト欠落・秘密値取得はexplicitDenyを確認した。追加Allowなしでもplan/applyそれぞれの対象2関数がallowedになる4ケースを確認し、計28ケースが成功した。この検証は架空のリソースARNと指定コンテキストでの評価であり、AWSポリシーの適用や実キーでの復号は行っていない。
- AWS未適用かつplan用SSOのNo accessが未解消のため、実CIロールのGetFunction/GetFunctionConfigurationによる読戻しと適用後の再planは未実施。

### LambdaのJSONログ初期化

Problem: Lambdaでも初期化失敗からアプリログをJSONで出力し、全体設定やLogfireへの依存を持ち込まずCloudWatchのログ経路に接続する。
Evidence: 既存のsetup_logfireはAPI・worker向けの全体設定と外部telemetryを初期化するため、Lambdaの最小設定とは責務が異なる。

`app/lambda_handlers/logging.py`のsetup_lambda_loggingをEmbeddingの同期handler先頭で実行する。structlogのINFO以上のイベントを、既存の項目にlevel・UTC timestampを付けた1行JSONとして標準出力へ書き出す。呼び出しごとに同じ設定を適用し、loggerをキャッシュしない。暗黙のcontextvars取り込みは行わず、メッセージ識別情報は既存の明示フィールドを使う。

全体設定・環境変数・Logfireの初期化を読み込まず、外部telemetry通信や標準loggingのハンドラー追加は行わない。EMFは引き続き専用処理が標準出力へ直接書き出し、JSONを二重に包まない。初期化ログの安全な工程・例外型と既存の業務ログ項目を維持する。通常のログ初期化障害は業務結果を変えず、キャンセルは伝播する。

Invariants: 初期化例外と部分バッチ応答、既存EMF形式・監査・通知を維持する。
Non-goals: API・Taskiq・relayのログ初期化、SDKやLambdaランタイム自身のログ制御、Logfire送信・トレース、Terraform・AWS適用は変更しない。
Done: 設定検証より前のJSONログ、連続呼び出しでの非重複、EMF形式維持、ログ障害時の結果維持を確認する。実環境のログ配送は後続で検証する。

検証結果（2026-09-10）: Ruff lint・format check、全単体テスト6,327件、make test-integrationの1,351件が成功。既存DB権限テスト22件はAlembic適用済みpublic.watchlist_entriesが必要なためスキップされた。単体件数はCIの必須プロキシ設定に関する回帰テスト2件を含む。ログ設定・標準出力の障害でも元の結果を保つこと、キャンセルの伝播、EMFの最上位構造維持を確認した。AWS実通信・配送検証は実施していない。

### HTTP設定の分離

Problem: Geminiが使う共通HTTP処理のimportでアプリ全体の必須設定を要求される依存を解消する。
Evidence: 外部HTTPファクトリがapp.config.settingsを参照し、BFF秘密情報やfrontend URLなどの読み込みを伴っていた。

`app/http/settings.py`のHttpSettingsへEGRESS_PROXY_URLのフィールドと検証を移した。全体設定Settingsも同じ型を継承する。EGRESS_PROXY_URLは必須とし、未設定・空文字・Noneを拒否する。内部namespaceの許可値はHTTP設定側の定義を通知先検証でも再利用し、許可範囲を変更しない。プロキシ条件は全環境で共通なので、HttpSettingsには環境区分を追加しない。

make_external_async_clientは生成時にHttpSettingsを作り、環境変数から値を取得する。import時の設定生成・キャッシュ・app.config参照をなくす。HTTP設定自身は.envを読み込まないため、外部実通信を行うすべての環境でEGRESS_PROXY_URLを渡す。既存の全体設定オブジェクトへの代入はHTTPの経路を変更しない。

Invariants: http/httpsと既存内部namespaceの検証、プロキシ必須の通信境界、SSRF・TLS・リダイレクト制御を維持する。設定不足ではtransport生成前に失敗し、直接接続へのフォールバックや呼び出し側proxy引数による迂回を許さない。GeminiのAPIキー取得と通信タイムアウトは既存の責務に残す。
Non-goals: ログ初期化、Terraform、デプロイ、新しい環境変数・依存パッケージ・通信制御の追加は行わない。
Done: 必要最小限の環境だけでEmbedding入口のimportとGeminiクライアント生成ができ、全体設定の読み込みが発生しないことと既存検証の互換性を確認する。

検証結果（2026-09-10）: Ruff lint・format checkはapp全体と変更テストで成功。全単体テスト6,316件、全統合テスト1,351件が成功し、既存DB権限テスト22件は必要なAlembic適用済みpublic.watchlist_entriesがないためスキップされた。別プロセスでapp.configのimportを禁止し、最小環境でのGemini生成とプロキシ指定、不正設定をimport時ではなく生成時に拒否することを確認した。外部AI・AWSへの実通信とデプロイは実施していない。

プロキシ必須化の検証（2026-09-10）: 未設定・空文字・Noneを拒否し、呼び出し側のproxy引数でも設定不足を迂回できないことを確認した。Ruff lint・format check、全単体テスト6,319件、全統合テスト1,351件が成功。既存DB権限テスト22件は上記と同じ理由でスキップ。DB・SSM経路、AWS設定・デプロイは変更していない。

### Lambda入口の配置整理

送信側は`app/lambda_handlers/outbox_relay/`、受信側は`app/lambda_handlers/embedding/`に配置する。各フォルダのhandler.pyが起動・組み立て・終了を担い、settings.pyに専用設定を置く。Embeddingのhandler.pyはバッチ検証、各レコードの本文検証・Consumer呼び出し・結果記録と部分バッチ応答までを進め、event.pyはJSON解析と共有イベント型への変換、composition.pyは工程別の依存配線を担う。SSM・DB・AIクライアントの準備と終了順序は共通の`app/lambda_handlers/article_analysis_lifecycle.py`が管理する。

各パッケージの__init__.pyからhandler関数を公開し、`app.lambda_handlers.outbox_relay.handler`と`app.lambda_handlers.embedding.handler`の起動パスを維持する。Terraformのcommandとイメージ選択処理は変更しない。初期化順序、空Records、ID不正の全体失敗、本文不正の個別失敗、監査・通知・通信設定・業務処理は維持する。

検証結果（2026-09-10）: Ruff lint・format check、全単体テスト6,292件、専用一時環境の`make test-integration`で1,351件が成功した。既存DB権限テスト22件はAlembic適用済みの`public.watchlist_entries`が必要なためスキップ。既存起動パスの関数解決と、relayがアプリ全体の設定を読み込まずにimportできることを確認した。ローカルにはLinux向け依存のawslambdaricがないためRuntime Interface Client経由の起動は未実施で、Pythonのモジュール・関数解決を検証した。AWSへの適用・デプロイは行っていない。

### SQS受信構造の共通化

Problem: SQSレコードの構造検証がEmbedding固有のモジュールにあり、配送形式と記事処理の責務が同居している。
Evidence: SqsRecord・SqsRecordBatchの検証、SQS入力エラーとEmbeddingの診断処理、既存の全体失敗・個別失敗テストを基準にする。

`app/lambda_handlers/sqs/records.py`にSqsRecord・SqsRecordBatch、`errors.py`にSqsInputError・SqsInputReasonを配置する。共通側はEmbeddingのイベント・Consumer・ログ処理を参照せず、受信構造の検証と本文の取り出しを担う。入力エラーのCODEは`sqs_input_invalid`とし、Embedding側のログイベント名・理由・診断項目は維持する。

Invariants: 全レコードのID検証を本文解析・Consumer呼び出しより先に行い、ID不正は全体失敗、本文の欠落・型不正は個別失敗とする。入力順序と元のmessageIdを保ち、エラーには入力値を保持しない。
Non-goals: イベント解析、Consumer呼び出し、失敗ID集約と応答型、業務処理、AWS設定は変更しない。
Done: 共通側がEmbeddingに依存せず、共通受信型の単体検証と既存の配送・ログ契約、バックエンドの単体・統合検証が通過する。

検証結果（2026-09-10）: Ruff lint・format check、Lambda関連の単体テスト193件、全単体テスト6,300件、専用一時環境の統合テスト1,351件が成功した。既存DB権限テスト22件は前提のDBスキーマ不足によりスキップ。共通受信型がEmbedding・アプリ設定を読み込まないこと、本文不正の個別処理、ID検証の先行と安全なエラーコードを確認した。

### Lambda実行手順の集約

Problem: 資源の準備とバッチの進行が別モジュールに分かれ、1回の実行手順を入口から通して読めない。
Evidence: handlerの資源管理、既存のバッチ・レコード処理、初期化・配送・終了障害と実Consumer接続のテストを基準にする。

handler.pyは公開handler、非同期_run_embeddingの順に配置する。同期入口で設定を読み、非同期処理で資源とConsumerを用意した後、SqsRecordBatch.from_lambda_eventで全IDを検証する。その場で入力順にprocess_embedding_recordをawaitし、FalseのmessageIdを応答へ集約する。応答型もhandler.pyに置く。旧sqs_batch_handler.pyとprocess_embedding_messagesは廃止し、1件の処理をrecord_handler.pyへ移す。

Invariants: 初期化後に全IDを検証し、本文不正は個別失敗とする。Consumer・資源を同じ呼び出し内で共有し、逐次処理と元のIDを維持する。初期化・全体失敗、個別失敗、キャンセルの伝播と、終了処理・安全な診断項目を維持する。
Non-goals: AWSの公開起動パス、Consumerの業務処理、イベント契約、SQS共通型、インフラ設定は変更しない。
Done: handler.pyで準備から応答・終了までを追え、既存の配送・資源管理テストを新しい入口へ接続して単体・統合検証が通過する。

検証結果（2026-09-10）: Ruff lint・format check、全単体テスト6,300件、専用一時環境の統合テスト1,351件が成功した。既存DB権限テスト22件は前提のDBスキーマ不足によりスキップ。旧バッチ関数のテストは資源の生成だけを差し替えて_run_embeddingへ接続し、実際のバッチ検証・逐次処理・応答集約を検証した。1件の処理と完了ログは関数名以外の構文が移動前と一致することも確認した。

### レコード検証と業務実行の順序を入口に明示

Problem: バッチの進行はhandler.pyに集約したが、1件のイベント検証とConsumer呼び出しが別関数に隠れ、検証後に実行する順序を入口から読めない。
Evidence: record_handler.pyの本文検証・完了判定・診断処理と、既存の配送・資源管理・実Consumer接続テストを基準にする。

_run_embeddingのループ内でrecord.body_text、parse_assessed_in_scope_event、consumer.consumeの順に呼ぶ。本文やイベントの不正は失敗IDを追加して次のレコードへ進み、Consumerを呼ばない。Consumerの通常例外も個別失敗とする。record_handler.pyとprocess_embedding_recordは廃止し、診断項目の詳細はfailure_recorder、検証ルールはSQS共通型とevent.pyに維持する。

Invariants: 資源準備後に全レコードの構造とIDを先に検証し、各本文を検証した後でのみConsumerを呼ぶ。逐次処理、元のmessageId、成功・失敗のログ項目、キャンセルの伝播、資源の終了を維持する。
Non-goals: 公開起動パス、設定・資源管理、業務イベント契約、ConsumerとSQS共通型の実装、AWS設定は変更しない。
Done: handler.pyだけで入力検証からConsumer実行・配送結果の集約までを追え、既存の単体・統合検証が通過する。

検証結果（2026-09-10）: Ruff lint・format check、全単体テスト6,301件、専用一時環境の統合テスト1,351件が成功した。既存DB権限テスト22件は前提のDBスキーマ不足によりスキップ。成功したレコードの後に不正本文が来てもConsumerを呼ばず、直前のイベント情報を診断へ流用せずに後続処理を続けることを追加検証した。

### 入力の段階を区別する命名

Lambdaの入力全体をlambda_event、検証済みレコード集合をrecord_batch、取り出した本文をmessage_body、解析済みの対象内判定イベントをassessed_eventとする。SqsRecordBatch.from_lambda_eventはLambda入力全体からレコード集合を復元し、parse_assessed_in_scope_event(message_body)は本文を共有のArticleAssessedInScopeEventへ変換する。SqsRecord.body_textは配送上の本文を取り出す役割として維持する。

変更は命名と参照更新に限定し、検証条件、エラーコードとログの項目名、実行順序、部分バッチ応答は維持する。

検証結果（2026-09-10）: Ruff lint・format check、全単体テスト6,301件、専用一時環境の統合テスト1,351件が成功した。既存DB権限テスト22件は前提のDBスキーマ不足によりスキップ。旧名称の参照が残っていないことと、パーサー・SQS構造検証・失敗診断の処理が命名以外では変わっていないことを確認した。

### 正常完了をEnumへ集約

Problem: 正常完了の型と理由が分かれ、ハンドラーの戻り値検証が処理の流れを読みにくくしている。
Evidence: ServiceとConsumerは保存完了または生成済みのみを正常結果として返し、失敗は例外として伝える。完了結果には理由以外の情報がなく、既存テストは保存・生成済み・競合・例外・ログ・配送応答を検証している。
Invariants: 保存・生成済みの区別、例外の伝播、逐次処理、失敗IDの集約、ログのreason値を維持する。
Non-goals: DB処理、イベント契約、資源管理、AWS設定は変更しない。
Done: EmbeddingCompletion自体をSAVEDとALREADY_EMBEDDEDのEnumにし、Consumerの正常戻り値をハンドラーが再検証せず記録する。旧型の参照とテストを更新し、単体・結合検証が通過する。

検証結果（2026-09-10）: Ruff lint・format check、全単体テスト6,298件、専用一時環境の結合テスト1,351件が成功した。既存DB権限テスト22件は前提のDBスキーマ不足によりスキップ。旧契約の不正戻り値5ケースを正常完了2種類の応答・ログ検証へ変更し、Service・Consumerの保存・生成済み・競合時のテストはEnum値を直接検証するよう更新した。

### 失敗項目の集約とAWS応答の組み立て

Problem: 内部で失敗項目を集め、入口で応答へまとめるという役割分担と、生成する項目・応答の型が処理箇所から読み取りにくい。
Evidence: 応答項目が持つのはmessageIdだけで、AWSのbatchItemFailures配列に含めることで失敗を報告する。既存テストは個別失敗・入力順・ID保持・空入力・資源終了を検証している。
Invariants: AWSのbatchItemFailuresとitemIdentifierの形式、失敗IDの入力順と原文、ログ、例外の伝播、資源終了を維持する。
Non-goals: Consumer、入力検証、AWS設定は変更せず、処理結果用のクラスは追加しない。
Done: _run_embeddingはlist[SqsBatchItemIdentifier]型のfailed_itemsを返し、公開handlerはその一覧をSqsBatchFailureResponseのbatchItemFailuresへ格納する。生成箇所でSqsBatchItemIdentifier(...)とSqsBatchFailureResponse(...)を明示する。内側の項目型は識別子を表し、失敗の意味は外側の応答で表す。単体・結合検証が通過する。

検証結果（2026-09-10）: Ruff lint・format check、全単体テスト6,301件、専用一時環境の結合テスト1,351件が成功した。既存DB権限テスト22件は前提のDBスキーマ不足によりスキップ。内部の識別子一覧、公開handlerの全件成功・一部失敗・全件失敗の応答、IDの順序と原文、資源終了を検証した。TypedDictの生成記法を明示した後も、既存の辞書形式の期待値を維持して検証が通過した。

### 失敗診断の記録責務を明示

Problem: FailureHandlerという名前が、ログ記録に加えて失敗一覧の更新や制御フローも担当するように読める。
Evidence: 対象クラスは診断項目の組み立てとログ出力だけを担当し、handler.pyとresources.pyから呼ばれている。
Invariants: ログのイベント名と項目、ログ障害の隔離、元の例外、失敗一覧への追加、continueとraiseの位置を維持する。
Non-goals: 失敗処理の責任移動、ConsumerとOutboxのFailureHandlerの変更は行わない。
Done: クラス・ファイル・変数・公開メソッドをFailureRecorder、failure_recorder、record_*の命名に揃え、旧参照を残さず単体・結合検証が通過する。

検証結果（2026-09-10）: Ruff lint・format check、全単体テスト6,301件、専用一時環境の結合テスト1,351件が成功した。既存DB権限テスト22件は前提のDBスキーマ不足によりスキップ。旧クラス・モジュールの参照が残っていないことを確認し、ログと配送結果の既存テストを変更せず通過した。

### スライス3.3：Lambdaハンドラーへの接続

Problem: 作成済みの接続・業務処理部品を、1回のLambda呼び出しとして実行して応答する入口を提供する。
Evidence: 共通のopen_article_analysis_consumer、Geminiのopen_gemini_client、新しいGeminiEmbedder、Consumerと既存SQS処理の契約を確認した。

`app/lambda_handlers/embedding/handler.py`の同期`handler(lambda_event, context)`がEmbeddingConsumerSettingsを生成し、asyncio.runで`_run_embedding(lambda_event, settings)`を実行する。contextは使用しない。非同期処理ではDB資源、Geminiクライアント、EmbedderとConsumerの順に初期化し、バッチ検証後に各レコードの本文を検証してConsumerを呼び、失敗IDを集約する。AsyncExitStackでGemini資源、DB資源の順に終了してから失敗項目の識別子一覧を返し、同期handlerでSqsBatchFailureResponseへまとめる。

同一呼び出し内で資源とConsumerを共有し、次の呼び出しには持ち越さない。DBは最初のSQLで接続し、接続確認SQLを追加しない。空Recordsも初期化を行ってから空の失敗一覧を返す。配送構造・本文の検証位置と引数型は変更せず、配送構造不正も初期化後の既存処理で拒否する。

初期化の通常例外は固定イベントembedding_initialization_failedへstage（settings・resources・gemini_client・consumer）とerror_classだけを記録し、元の例外を伝播する。SSM・DBはresourcesとして扱い、内部工程を分ける新しい例外や公開インターフェースは設けない。本文・秘密情報・設定値・例外自由文・トレースバックを新設ログに渡さず、ログ障害でも元の例外を維持する。

初期化失敗は空入力でも呼び出し全体の失敗となる。配送構造不正や処理中の例外を初期化失敗として重複記録しない。通常の終了障害は既存の資源管理で記録し、確定済み応答や先行例外を変更しない。キャンセル・プロセス終了は伝播する。個別の業務失敗監査・通知・計測は既存Consumerへ任せる。

Invariants: 既存検証・部分バッチ応答、Consumerの60秒、接続設定、セッション境界と成功監査の非重複を維持する。
Non-goals: Taskiq・relay・DB schema・依存パッケージ・設定スイッチ・トレース基盤の変更、SQS直接操作、Terraform・実行イメージ・デプロイは行わない。
Done: 呼び出し単位の初期化と終了、空入力、個別失敗・全体失敗、実DB保存と監査の非重複を検証する。Lambda全体120秒とReportBatchItemFailuresの基盤設定は3.4、AWS実通信検証は後続に残す。

検証結果（2026-09-10）:

- Ruff lint・format checkはapp全体と変更テストで成功した。全単体テストは6,286 passed。
- `make test-integration PYTEST_ARGS='-rs -x'`は1,351 passed・22 skipped。既存DB権限テスト22件はAlembic適用済みの`public.watchlist_entries`が検証環境にないためスキップされた。
- 同期入口からの連続呼び出し、資源共有と再生成、空入力・配送構造不正の初期化後の処理、初期化ログの項目制限、終了・ログ障害時の結果維持、キャンセル・プロセス終了の伝播を確認した。
- 実DB・実SDK・新しいEmbedder・実Consumerを組み立て、API障害後も後続メッセージが保存されること、同一呼び出しと次の呼び出しでの生成済み再処理がAPI・成功監査を重複させないことを確認した。HTTP通信とSSM取得はモックした。
- AWS実通信、Lambda全体120秒、SQSトリガー、ReportBatchItemFailuresの実環境での動作、デプロイは今回未実施で、後続スライスで扱う。

### スライス3.2：SSM取得とDB資源の組み立て

Problem: APIキーを毎回取得し、DBの物理接続を呼び出し内だけ再利用する準備部品を提供する。
Evidence: 既存DB設定・TLS変換・IAM provider・セッション生成器とConsumerのセッション境界を利用する。

`EmbeddingConsumerSettings`はenv・aws_region・database_url・db_iam_auth・gemini_api_key_parameter_pathだけを読み込み、`.env`を使用しない。productionではIAM認証とTLSを必須にし、IAM URLのパスワード禁止を維持する。SSMパスには既存Terraformの専用パス出力を渡す。APIキーの実値は設定に保持しない。

`app/aws/ssm.py`の`get_secret_parameter`はGetParameterのWithDecryption=Trueで取得し、非空文字列をSecretStrとして返す。SSMは標準リージョンエンドポイントと資格情報provider chainを使用し、AI用プロキシと任意の環境由来エンドポイントを使用しない。接続3秒・読み取り5秒・standardモードで最大2試行とし、キャッシュ・独自再試行を設けない。同期取得処理自身がSSMクライアントを閉じる。

`create_embedding_consumer_engine`は既存TLS・パラメーター非表示・pre_pingを維持し、1接続・追加接続0・取得待ち5秒・接続5秒・コマンド5秒でEngineを生成する。application_nameはvector-embedding-consumerとし、SQLを実行するまで接続しない。IAM署名関数を注入できるよう既存providerの引数を追加し、新しい経路だけが呼び出し内で所有するRDS SDKクライアントを使う。従来のprovider呼び出しは変更しない。

`open_embedding_consumer(settings)`は、設定を捕捉する型付きの名前付き関数でEngine・Geminiクライアント・Consumerの生成を指定し、共通の`open_article_analysis_consumer`が返すasync context managerを利用する。共通側はSSM取得をスレッドへ委譲した後、RDS署名器・Engine・session factory・AIクライアント・Consumerの順に準備する。同期SSM要求中のキャンセルは通信自体を中断しないが、スレッド内でSSMクライアントを閉じる。各処理でセッションを閉じてから利用範囲を終了し、AIクライアント、Engine、RDSの順に解放する。SDK内部の生成・解放は`open_gemini_client`が所有する。共通入口はクライアント型とConsumer型をジェネリクスでつなぎ、工程別入口の戻り型は`AbstractAsyncContextManager[EmbeddingConsumer]`とする。Recorderは初期化・終了の2メソッドだけを要求するProtocolで受け取り、既存実装を維持する。

初期化途中でも生成済み資源を閉じる。通常の終了失敗は固定イベント名・資源種別・例外型だけを記録し、元の結果や例外を置き換えず他の資源の終了を試みる。キャンセル・プロセス終了は伝播する。初期化失敗にConsumerの監査・通知・SQS応答を流用しない。

Invariants: 取得・保存・失敗監査のセッションとトランザクション境界、既存Taskiq・relayの動作、IAM・TLSの条件を維持する。
Non-goals: DB schema・新規依存・Terraform・SSM値登録・AWS適用・Consumer本番配線の変更は行わない。
Done: 毎回のキー取得、同一呼び出し内の接続再利用、終了処理と失敗後の接続回復を検証できること。Gemini・Consumer組み立てとLambda入口は3.3、実環境のSSM復号・RDS IAM接続は後続で実証する。

検証結果（2026-09-10）:

- Ruff lint・format checkはapp全体と変更したテストで成功した。
- 全単体テストは6,266 passed。実Botocoreとモック通信による最大試行数、秘密値の非表示、初期化・終了・ログ障害、キャンセル時の資源終了を確認した。
- `make test-integration PYTEST_ARGS='-rs -x'`は1,349 passed・22 skipped。既存のDB権限テスト22件はAlembic適用済みの`public.watchlist_entries`が必要なためスキップされた。
- 実DBで接続再利用、トランザクション分離、プール取得・SQLの時間切れ、切断後の再接続、利用範囲終了時の接続終了を確認した。実Consumerで保存・生成済み再処理・API障害後の監査が重複しないことを確認した。
- 初回の統合検証では切断要求直後の再接続テストが切断通知処理と競合したため、テストを切断完了通知後に再接続する形へ修正し、全統合テストを再実行した。本番コードに待機・独自再試行は追加していない。
- 外部AI・AWSはモックし、実環境のSSM復号・RDS IAM接続、デプロイは実施していない。


### スライス3.1b：借用クライアントを使うGemini Embedder

`app/analysis/embedding/embedder.py`に新しい`GeminiEmbedder(*, client: google.genai.client.AsyncClient)`を追加した。旧GeminiEmbedderを継承・呼び出しせず、既存BaseEmbedder契約のサブクラスとしてConsumerに渡せる。共通クライアントの生成・終了、APIキー取得、通信設定、再試行は呼び出し元の責務とする。

モデル・次元・task type・prefixは既存のGEMINI_EMBEDDING_SPECを使う。借用した非同期クライアントでembed_contentを一度呼び、空応答はEMPTY_EMBEDDINGS、先頭のvalues欠落はMISSING_VALUESとして既存のAIProviderRequestInvalidErrorへ変換する。複数応答では既存契約どおり先頭を使用する。数値配列はBaseEmbedderとEmbeddingVectorを通して次元・有限性・許容範囲を検証する。

API例外は既存translate_gemini_errorへ委譲し、未分類例外とキャンセルは伝播する。監査・計測・通知を追加せず、Consumer・Serviceの既存処理を利用する。既存GeminiEmbedder・Taskiq・Consumerの本番配線は変更していない。SSM・DBの組み立てとLambda接続は後続、旧実装の削除は移行後に扱う。

### スライス3.1：Gemini共通クライアント

`app/ai_providers/gemini/settings.py`の不変な`GeminiConnectionSettings`に、正の有限値である接続・読み取り・書き込み・プール待ち時間を宣言する。APIキー・モデル・次元は含めず、新しい環境変数は追加しない。

`open_gemini_client(*, api_key: SecretStr, settings: GeminiConnectionSettings)`は非同期コンテキストマネージャーとしてGemini SDKの非同期クライアントを提供する。APIキーは外から受け取り、空白だけの値を生成前に固定メッセージで拒否する。SSM取得・キーのキャッシュ・業務例外への変換は担当しない。

既存の外部HTTPファクトリを使ってプロキシ・TLS・宛先検証・リダイレクト制御を維持する。Gemini Developer APIを明示し、SDKの試行回数を1、HTTP transportの再試行を0とする。SDKによる要求単位のtimeout上書きに対応するため、非同期request hookで送信直前のtimeout extensionへ設定値を適用する。

所有するHTTPクライアント、SDK同期側・非同期側は利用範囲終了時に閉じる。初期化途中の失敗でも取得済み資源を閉じ、通常の終了失敗は`gemini_client_cleanup_failed`に資源種別と例外型だけを記録する。終了・ログの通常障害は利用結果や元の例外を変更せず、残りの資源の終了を試みる。キャンセル・プロセス終了は伝播する。

3.1では既存GeminiEmbedder・Taskiq・Consumerを変更していない。新しいEmbedderは3.1bで追加し、SSM・DB・Lambdaの組み立ては3.2以降で行う。呼び出し内での共有は呼び出し元が利用範囲を設定し、共通クライアント自身はグローバルなインスタンスを持たない。


スライス1のTerraformを実装した。embedding元キューのみ保持4日・可視性720秒・受信上限5回に変更し、保持14日の専用DLQを紐付ける。DLQはembedding元キューだけからredriveを許可し、非TLSを拒否する。

Consumer専用サブネットはprimary AZのCIDR index 28とし、appルートテーブルを使用する。専用SGはRDS・proxy・SSMだけに接続し、proxyはGeminiのみ許可する。実行ロールとbootstrapの専用boundaryは元キュー受信・対象DB・専用Geminiパラメーター読取・専用ログ・LambdaのENI管理に限定する。CIのDLQ管理権限とrelayの送信権限は分離する。

DLQ滞留通知は`ApproximateNumberOfMessagesVisible`のMaximum・60秒・1評価期間・1件以上・欠測正常で判定し、ALARM/OK遷移を既存SNSへ送る。自動停止・自動再投入は行わない。障害時は後続のSQSトリガーを手動停止・再開する。

Consumer本体とLambda起動関数は実装済み。スライス3.4でLambda関数・無効状態のSQSトリガーのTerraform定義を追加したが、AWSには未適用。スライス2に先行して、共通Serviceの保存時行ロックと記事不存在・生成済みの区別を実装した。Serviceは正常終了時に`EmbeddingCompletion.SAVED`または`EmbeddingCompletion.ALREADY_EMBEDDED`を返す。Service実行中の失敗分類関数とConsumer用の後処理ハンドラーも実装済み。開始時の失敗もConsumer用ハンドラーへ接続した。SQSの入力検証・Consumer呼び出し・部分バッチ応答も処理部品として接続済み。依存を組み立てるLambda起動関数は3.3で接続済み。

## Verification

共通クライアントを使う新しいGemini Embedder（2026-09-10）:

- 新規単体テスト20件で要求内容・正常ベクトル・空応答・values欠落・不正ベクトル・複数応答・SDK障害分類・未分類例外・キャンセル・借用クライアントの再利用を確認した。共通クライアントと実SDKをモックHTTPへ接続し、接続3秒・読み取り10秒などの実効設定も確認した。
- 実Consumerと実DBの追加3ケースで、保存と成功監査の一回性、生成済み再処理時のAPI非呼び出し、API障害と応答不正時の失敗監査・例外伝播を確認した。
- Ruff lint・format check（変更・追加テストを含む）、全単体テスト6,231件、`make test-integration PYTEST_ARGS='-rs'`の1,343件が成功した。22件skipは既存DB権限テストに必要なAlembic適用済み`public.watchlist_entries`が一時DBにないため。
- 初回の単体テスト実行は、SDK用fixture名`client`が既存のDBテスト分類に該当して未起動DBへ接続したため終了した。`sdk_client`へ命名を修正して単体・一時DBの全integrationを実行した。
- 外部AI・AWSはモックし、既存Taskiq・旧Embedder・Consumer本番配線は変更していない。実運用切り替えとデプロイは未実施。

Gemini共通通信設定・クライアント管理（2026-09-10）:

- 追加テスト45件で、実SDKが送る要求のtimeout、429・5xx・通信障害時の単一試行、呼び出し範囲内の共有と範囲間の分離、初期化途中の失敗、通常の終了障害・ログ障害・キャンセルと資源解放を確認した。
- Ruff lint・format check（追加テストを含む）、全単体テスト6,211件が成功した。
- `make test-integration PYTEST_ARGS='-rs'`は1,340件成功・22件skip。skipは既存DB権限テストに必要なAlembic適用済み`public.watchlist_entries`が一時DBにないため。
- 通信先はモックし、AWS・外部AIの実通信、既存GeminiEmbedder・Taskiq・Consumerの変更、デプロイは行っていない。

必要な検証:

- 正常時にベクトルと成功監査を同一トランザクションで保存する。
- 開始時の生成済み判定ではAIを呼ばない。
- Taskiqとconsumerの競合では1つの保存結果と成功監査だけが確定する。
- 開始時の記事不存在、処理中の削除、更新0件の再確認失敗を成功にしない。
- コミット後に応答前の終了・接続終了失敗が起きて再配信されても、生成済み判定で吸収できる。
- 不正イベント、API障害、DB障害、60秒の処理上限を失敗として扱う。
- 失敗記録の二次障害で元の失敗を成功に変えない。
- 失敗時に直接DLQ送信やメッセージ削除を行わない。
- AWS上で受信数・同時実行・可視性timeout・redrive policyの設定と動作を確認する。

スライス1の検証（2026-09-09）:

- 本体・bootstrapのfmt check・backend未接続init・validateを実施し、すべて成功した。本体には既存Cloud Mapの`failure_threshold`非推奨警告が残る。
- AWS provider mockテストは本体4件・bootstrap3件が成功した。再配信と送信先の分離、専用ネットワーク、実行権限とboundary、PassRole制約、通知設定を検証した。
- SSOログイン後に`vector-plan`で実環境のread-only planを実施した。本体は18追加・4更新・1削除、bootstrapは1追加・2更新・削除なし。既存キューの再作成はなく、削除はproxyタスク定義の新revisionへの置き換えだけ。他工程のキューは変更なしで、Consumer関数・SQSトリガーの追加もない。relayのSchedulerはDISABLEDを維持する。
- 本体planの更新には既存relay Lambdaの環境変数・image_configが含まれる。planロールの既存`kms:Decrypt`明示DenyによりLambda APIが両項目を返せず、`AccessDeniedException`を返すことを確認した。この2項目は実設定と正確に比較できず、実際の設定差分とは断定しない。権限制約の変更や迂回は行っていない。適用前に既存の承認経路で確認する。
- AWS適用・SSMの実値登録・実通信・通知配送・再配信とDLQ移動の実証は未実施。
- backend・frontendコードを変更していないため、それらの全テストは実施していない。Consumer本体の実装時には`/check`のbackend検証を実施する。

保存時の状態区別の検証（2026-09-09）:

- Ruff lint・format checkは実行コードと変更テストで成功した。
- 全単体テスト5,975件、状態型の変更後のembedding単体テスト115件が成功した。
- `make test-integration`は1,300件成功・22件スキップ。既存のDB権限テストにはAlembic・Better Auth schemaを必要とするスキップ条件がある。
- 実DBで処理中の削除、同時実行時の保存・成功監査の一意性、ロック待機失敗、コミット失敗時のロールバックを確認した。生成済みでreturnした直後の別セッションによるNOWAIT行ロック取得も、追加の統合テストで成功した。
- 外部AIはモックした。ConsumerとTaskiqの実併用・SQS応答・AWSでの実証は後続スライスに残る。

正常完了・失敗理由の契約変更の検証（2026-09-09）:

- Ruff lint・format checkは実行コードと変更テストで成功した。全単体テスト5,979件、最後に更新したTaskiqの例外伝播テスト7件が成功した。
- `make test-integration`は1,300件成功・22件スキップ。22件はいずれも既存のDB権限テストで、Alembic適用済みの`public.watchlist_entries`が検証環境にないためスキップされた。
- 正常保存・生成済み・同時実行時の`EmbeddingCompletion`、記事不存在・応答不正・provider障害の理由と原因保持、既存Taskiqの監査分類・再試行・hold・通知providerを確認した。
- 監査のerror_chainにTaskiq分類・Service失敗・元のprovider例外が残ることを実DBで確認した。DB障害・想定外例外は既存の伝播を維持する。
- Consumer本体・SQS失敗応答・デプロイは今回の対象外。外部AIはモックした。

Consumer用の失敗分類・後処理の検証（2026-09-09）:

- Ruff lint・format checkが成功した。全単体テスト5,999件、`make test-integration`の1,311件が成功した。既存のDB権限テスト22件は、Alembic適用済みの`public.watchlist_entries`が検証環境にないためスキップされた。
- provider全分類、Serviceの理由、DB障害、想定外例外の監査・監視・通知対象を単体テストで確認した。分類関数は副作用を実行せず、例外の原因連鎖を変更しない。
- 実DBで監査payload・元の例外と原因連鎖・秘匿処理を確認した。監査の外部キー違反後にも通知が実行されること、計測・通知・ログの二次障害が元の例外を置き換えないことを確認した。
- 既存の枯渇通知とTaskiqの検証も全テストに含めた。AWS通知配送・Consumer本体への接続・SQS応答・デプロイは未実施で後続の対象。

Consumer本体の検証（2026-09-09）:

- Ruff lint・format checkが成功した。全単体テスト6,003件、`make test-integration`の1,334件が成功した。既存のDB権限テスト22件は、Alembic適用済みの`public.watchlist_entries`が検証環境にないためスキップされた。
- 外部AIをモックし、実DBで正常保存、開始時の生成済み・記事不存在、Ready入力拒否、AI処理中の削除、API障害、ロック待機失敗、コミット失敗を確認した。開始状態の取得が一度だけで、AI実行前にDB接続を返却することも確認した。
- Consumer同士、およびConsumerと実際の既存Taskiqタスクの並行実行で、ベクトルと成功監査が一度だけ確定することを確認した。
- 本番の期限が60秒であることを確認し、テストでは短い期限で対象取得中・AI実行中の時間切れを再現した。期限後にも失敗監査を保存し、取得できなかった元記事IDはNULLとして分析記事IDをpayloadに残すことを確認した。
- 分類・後処理・監査・ログの二次障害で元の例外を置き換えないこと、外部キャンセルを通常の失敗として記録しないことを確認した。
- Lambda入口・SQS応答・SDK設定・デプロイは今回の対象外。AWS上の実通信・再配信・DLQ移動は後続スライスで検証する。

共有イベント契約・本文検証の検証（2026-09-09）:

- 実行コードと変更テストのRuff lint・format check、`git diff --check`が成功した。全単体テストは`pytest tests/ -m 'not integration' -x -q`で6,071件成功した。
- `make test-integration PYTEST_ARGS='-rs'`は1,336件成功・22件スキップ。既存DB権限テスト22件は、Alembic適用済みの`public.watchlist_entries`が検証環境にないためスキップされた。
- 既存本文の復元、送信本文の往復、厳密な型検証、不正理由の優先順位、重複キー・非標準JSONの拒否、例外文面と原因連鎖への入力非保持を確認した。
- 実DBと実publisherを使い、未対応バージョン・不正payloadだけがOutboxの配信停止となり、正常イベントは配信済みになることと、次回relayで再送されないことを確認した。AWSクライアントはモックした。
- 単体・統合を分けない初回pytestは、未起動のローカルDBへの接続で終了したため、単体の明示選択と隔離DBでの全統合テストに分けて完了した。AWS実送信・Lambda接続・デプロイは今回の対象外。

SQS入力検証・Consumer接続の検証（2026-09-10）:

- 実行コードと変更テストのRuff lint・format check、`git diff --check`が成功した。最終状態の全単体テストは6,149件成功した。
- `make test-integration PYTEST_ARGS='-rs'`は1,340件成功・22件スキップ。既存DB権限テスト22件は、Alembic適用済みの`public.watchlist_entries`が検証環境にないためスキップされた。
- 共有の項目・コードと大分類、未知キーの非公開と重複排除、送受信の一致、SQS構造不正の事前拒否、全件成功・一部失敗・全件失敗・空配列、逐次実行、契約外の戻り値、ログ障害、キャンセル伝播を確認した。
- 実Consumerと実DBで、保存・生成済み・本文不正・記事不存在・API障害を部分バッチ応答へ反映することと、保存・監査・計測を入口で重複しないことを確認した。外部AI・AWSはモックした。
- 検証条件・DB schema・Consumerの業務契約・Taskiq・AWS設定は変更していない。Lambda起動関数の依存構築、ReportBatchItemFailuresの有効化、AWS実通信・デプロイは後続とする。

## 実装・有効化前に確定する項目

- 呼び出し内での資源・APIキー共有、呼び出し間の非共有、初期化後の入力検証、初期化・終了失敗の方針と入口の実装は3.3までに反映した。AWS上の有効化は後続とする。
- Geminiの通信値と終了方法は3.1で確定した。SSM・DBの通信値とプール方式は3.2で確定した。
- 3.4のLambdaメモリ1024MB、独立digest指定、専用サブネット・SG・権限の定義は実装済み。bootstrap先行適用、イメージ公開、本体適用と無効状態の確認を完了した。受信有効化はスライス4.1の対象とする。
- AWS上での構造化ログ、部分バッチ応答、通信・再配信・DLQ移動とTaskiq併用をスライス4で確認する。
- 共通障害・設定不備が継続した場合の手動停止・再開手順はスライス4.1で整備する。復旧後のDLQ再投入手順と実証は後続とする。DLQ滞留通知は実装済みで、自動停止は行わない。既存holdはSQS起動トリガーを停止しない。

未確定の詳細は、合意済みの成功・失敗方針と設定値を変更する理由にはせず、該当部分の実装前に仕様を更新する。


## 共通ライフサイクルの初期化診断

初期化の診断段階は共通側で`resources`・`ai_client`・`consumer`に固定し、工程側から指定しない。工程別のログ名は既存のRecorderが引き続き担当する。

## Consumer資源準備のIAM契約（2026-09-11）

Consumer設定は環境に関係なくIAM認証を必須とする。`create_embedding_consumer_engine`はIAM署名器を必須引数として受け取り、省略・Noneを拒否する。資源準備は常に呼び出し専用のRDS署名器を生成し、テスト向けの非IAM分岐は持たない。実DBテストでは製品側の経路を維持し、AWS署名器だけをローカルDB認証情報を返すものへ差し替える。本番の接続方式とTLS要件は変わらない。

検証: app全体と変更テストのruff lint・format、全単体6,565件、全統合1,402件が成功（22件skip）。
