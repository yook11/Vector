# EmbeddingConsumer — SQS受信とベクトル生成

Status: Draft（2026-09-09、スライス1のTerraform実装済み・AWS未適用）

業務上の成功・失敗、再配信設定、Taskiqとの併用方針は合意済み。受信adapter・失敗記録・Lambda実行の詳細は末尾の未確定項目に分ける。基盤のTerraform実装と実環境での有効化を区別して記録する。

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
- [relay基盤](../../infra/aws/outbox_relay.tf)：Standardキュー、relay Lambda、無効状態のScheduler。consumer・SQS起動トリガーは未実装。
- [Consumer基盤](../../infra/aws/embedding_consumer.tf)：専用サブネット・IAM・SSM経路・DLQ・通知。
- [適用手順](../../infra/aws/README.md#embeddingconsumer基盤の追加スライス1)：bootstrap先行・滞留確認・秘密情報登録・後続検証。
- AWS公式：[SQSとLambdaの接続設定](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)、[同時実行制御](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)、[DLQ](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)。

現状の記載はリポジトリに基づく。AWS実環境の稼働・適用状況は未確認。

## Invariants

- 保存完了とは、ベクトルと成功監査のトランザクションをコミットできたことを指す。
- 「例外なくreturnした」ことだけを根拠にSQSへ成功を返さない。
- 開始時に生成済み、または別の実行が先に保存したことを確認できた場合は対応完了とする。
- 対象記事不存在は失敗とし、処理不要による成功にはしない。
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

受信本文は`app/lambda_handlers/embedding_event.py`の`parse_embedding_event(body: str)`で解析し、同じ共通型を返す。後続のハンドラーはevent_id・occurred_atを追跡情報として保持し、payloadだけをConsumerへ渡す。JSONの重複キーとNaN・Infinityを拒否する。

不正本文は`EmbeddingEventInvalidError`で伝える。理由はJSON解析の`invalid_json`、外側の構造・項目型の`invalid_envelope`、対象外種別の`unsupported_event_type`、未対応版の`unsupported_schema_version`、payload内部の`invalid_payload`の順で優先する。payload自体の欠落や非オブジェクトは外側の構造不正に含む。共有のassessed_event_validation_failureは、大分類と重複のない不変の検証詳細を返す。詳細は既知の項目名と固定コードだけとし、未知キーはeventまたはpayloadのunknown_fieldへ置き換える。本文・入力値・検証自由文を属性や原因・contextに保持せず、本文解析関数内ではログ・監査・通知を行わない。JSON解析失敗はinvalid_jsonと空の詳細一覧で返す。

送信前の契約違反は既存の`PublishEventInvalidError`と個別の`PublishFailed`へ変換し、不正イベントだけをOutboxの自動配信停止へ進める。正常な同一バッチのイベントは送信する。これは受信後のSQS再配信やDLQ移動とは別の処理である。既存のpublisher呼び出し契約違反・送信先判定は維持し、日時不正も共有契約のinvalid_envelopeとして扱う。

実装済みは共通型・送信前検証・本文解析と、次節のSQSメッセージ処理まで。依存を組み立てるLambda起動関数とイベントソースマッピングは後続とする。

### SQSメッセージ処理と部分バッチ応答

`app/lambda_handlers/embedding.py`の`process_embedding_messages(event: object, *, consumer: EmbeddingConsumer)`は、呼び出し元が組み立てたConsumerを使用する。SSM・Engine・AIクライアントの生成やSQSへの直接操作は行わない。

最初に入力がオブジェクト、Recordsが配列、各レコードがオブジェクトであることを確認する。全messageIdの存在・文字列型・空白だけでないこと・重複がないことをConsumer実行前に確定する。構造不正はEmbeddingSqsInputErrorとして呼び出し全体へ伝え、ログには固定の項目名・理由・0始まりのレコード位置だけを記録する。不正なIDそのものは記録しない。

有効なmessageIdは加工せず保持し、入力順に1件ずつbodyを検証してConsumerへpayloadを渡す。使用しないSQSフィールドは許容する。bodyの欠落・非文字列・本文不正・Consumerの通常例外・契約外の戻り値は個別失敗として後続処理を続ける。EmbeddingCompletionのSAVED・ALREADY_EMBEDDEDだけを正常完了とする。キャンセル・プロセス終了は伝播する。

戻り値は常にSqsBatchResponseの辞書形式で、失敗IDだけを入力順に含める。全件成功・空のRecordsは`{"batchItemFailures": []}`、失敗時は`{"batchItemFailures": [{"itemIdentifier": "失敗したmessageId"}]}`とする。複数件という理由では拒否せず、内部並列処理は追加しない。

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
| 開始時に対象記事が存在しない | 対象記事不存在の失敗を記録 | SQSへ失敗を返す |
| AI処理中に対象記事が削除され、保存できない | 対象記事不存在の失敗を記録 | SQSへ失敗を返す |
| 入力不正・未対応のイベント、実APIの拒否・429・通信障害・5xx | 失敗を記録 | SQSへ失敗を返す |
| 利用枠枯渇・残高不足・設定不備 | 失敗を記録し、既存の該当する通知を維持 | SQSへ失敗を返す |
| DB処理・コミットの失敗、処理時間上限到達、想定外例外 | 失敗として扱う | SQSへ失敗を返す。強制終了時も成功応答しない |

SQSによるメッセージ削除はLambda連携の成功処理に任せる。consumerから個別の削除APIは呼ばない。

失敗監査自体がDB障害で保存できない場合も成功にはしない。Lambdaの強制終了ではアプリケーションの失敗記録を実行できない可能性があるため、Lambda側の失敗観測も必要とする。

### 保存時の状態確認

共通の`EmbeddingService`はAI処理を終えた後に保存用トランザクションを開始し、記事IDで`SELECT ... FOR UPDATE`して保存対象をロックする。Repositoryの`lock_save_state()`は、存在と生成状態を`EmbeddingSaveState`の3状態として返す。

- `ARTICLE_MISSING`：`EmbeddingAnalyzedArticleMissingError`を送出する。
- `EMBEDDED`：`EmbeddingCompletion(reason=ALREADY_EMBEDDED)`を返し、成功監査は重複させない。
- `UNEMBEDDED`：条件付きUPDATEと成功監査を同一トランザクションでコミットした後に`EmbeddingCompletion(reason=SAVED)`を返す。

行ロックはAI待機中には保持せず、保存時からトランザクション終了まで保持する。記事の削除が先に確定した場合は不存在となり、保存側が先にロックした場合は削除が待機する。ロック取得・更新・コミットの失敗は呼び出し元へ伝播する。ロックした未生成行の更新が0件になる矛盾も正常終了させない。

`EmbeddingSaveState`は保存前のDB状態、`EmbeddingCompletion`はServiceの正常終了結果を表す。記事不存在・API障害・DB障害は結果値に変換せず例外で伝える。正常完了のreasonは`EmbeddingCompletionReason`とする。

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
2. 取得用セッションを閉じ、`ReadyForEmbedding.from_facts()`で純粋に開始条件と入力を検証する。Taskiqの既存`try_advance_from()`もこの関数へ委譲し、戻り値・blocked例外・hintの優先順位を維持する。
3. 開始時に不存在なら`EmbeddingAnalyzedArticleMissingError`へ変換する。生成済みならAI・成功監査・成功計測を行わず`ALREADY_EMBEDDED`で完了する。
4. 未生成ならServiceを実行して`EmbeddingCompletion`をそのまま返す。保存時の競合・削除・コミット失敗の契約を維持する。
5. 開始時からService完了までの通常の例外は、分類・後処理を経て再送出する。分類や後処理自体の予期しない二次障害でも元の例外を維持し、安全なログを試みる。外部キャンセルは通常の失敗として処理しない。

元記事IDを取得できなかった場合は`article_id=NULL`として分析記事IDをpayloadに残す。開始時の不存在も`embedding_analyzed_article_missing`／`target_missing`のFAILED監査とする。取得済みの記事IDはそのまま使用し、後から親記事が削除されて監査不能になった場合も既存のdrop計測・通知を試み、元の例外を伝播する。

今回は既存Taskiqの入口や配置を移動せず、Consumer専用トレースの配線・Lambda起動関数・デプロイは後続に残す。

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
4. 実環境で接続確認後に受信を有効化し、実行・再配信・DLQ移動を検証してTaskiqとの併用を開始する。

Doneは、正常処理・生成済み・競合でメッセージが対応完了となり、失敗が記録され、再配信上限後にDLQへ移り、Taskiqとの重複でDB結果を壊さないことを検証できた状態とする。ローカル実装完了と、AWS上での有効化・検証完了は区別して記録する。

## Implementation

スライス1のTerraformを実装した。embedding元キューのみ保持4日・可視性720秒・受信上限5回に変更し、保持14日の専用DLQを紐付ける。DLQはembedding元キューだけからredriveを許可し、非TLSを拒否する。

Consumer専用サブネットはprimary AZのCIDR index 28とし、appルートテーブルを使用する。専用SGはRDS・proxy・SSMだけに接続し、proxyはGeminiのみ許可する。実行ロールとbootstrapの専用boundaryは元キュー受信・対象DB・専用Geminiパラメーター読取・専用ログ・LambdaのENI管理に限定する。CIのDLQ管理権限とrelayの送信権限は分離する。

DLQ滞留通知は`ApproximateNumberOfMessagesVisible`のMaximum・60秒・1評価期間・1件以上・欠測正常で判定し、ALARM/OK遷移を既存SNSへ送る。自動停止・自動再投入は行わない。障害時は後続のSQSトリガーを手動停止・再開する。

Consumer本体は実装済み、Lambda・SQSトリガーは未実装。スライス2に先行して、共通Serviceの保存時行ロックと記事不存在・生成済みの区別を実装した。Serviceは正常終了時に`EmbeddingCompletion(reason=SAVED)`または`EmbeddingCompletion(reason=ALREADY_EMBEDDED)`を返す。Service実行中の失敗分類関数とConsumer用の後処理ハンドラーも実装済み。開始時の失敗もConsumer用ハンドラーへ接続した。SQSの入力検証・Consumer呼び出し・部分バッチ応答も処理部品として接続済み。依存を組み立てるLambda起動関数は後続で扱う。

## Verification

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
- 正常保存・生成済み・同時実行時の`EmbeddingCompletion.reason`、記事不存在・応答不正・provider障害の理由と原因保持、既存Taskiqの監査分類・再試行・hold・通知providerを確認した。
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

- Lambda起動関数での依存の組み立てと、実装済みprocess_embedding_messagesへの接続。
- 入力不正は構造化ログに記録する方針で実装済み。AWS上でのログ出力と部分バッチ応答の動作を有効化時に確認する。
- SDK timeout・内部再試行の設定、Lambda側のDB・AIクライアントの生成・終了方法。
- Lambdaのメモリ、SSM取得・キャッシュの実装、専用サブネット・SGをLambdaへ接続する配線。基盤の専用権限を利用し、relayの権限は流用しない。
- 残高不足・設定不備が継続した場合の手動停止・復旧・再投入の具体的な操作手順。DLQ滞留通知は実装済みで、自動停止は行わない。既存holdはSQS起動トリガーを停止しない。

これらの未確定事項は、合意済みの成功・失敗方針と設定値を変更する理由にはせず、該当部分の実装前に仕様を更新する。
