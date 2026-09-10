# Outbox relay Lambda基盤

## 現在の実装範囲

relayは既存maintenance workerへ同居させず、専用Lambdaで実行する。共通backendコンテナイメージを利用し、Terraformで起動コマンドだけを切り替える。Dockerfileと既存ECSの起動方法・権限は変更しない。

handlerは起動ごとに設定・DB Engine・publisher・failure handlerを組み立て、OutboxRelay.run_onceを1回実行してEngineを終了する。正常応答は今回の実行完了を意味し、全件の送信成功やconsumerの処理完了を意味しない。

```json
{"status": "completed"}
```

ベクトル生成向けイベントの停止最大100件・確保最大10件・SQSバッチ送信最大1回と、イベントごとの結果記録を接続済みである。送信対象は `article.assessed_in_scope` のみで、他工程のイベントは取得しない。初期構築ではSchedulerを `DISABLED` で配置した。スライス4.2の目標状態は `ENABLED` とし、既存リソースの更新・今後の新規作成ともに定期送信を有効にする。Consumerの有効な受信と既存Taskiqの並行稼働を維持する。

## リソースと境界

- 同一アカウント・リージョンに工程別のStandardキューを4つ作成する。
- 全キューをSSE-SQSで暗号化する。embedding元キューは保持4日・可視性720秒・受信上限5回、専用DLQは保持14日、他3工程のキューは保持14日とする。
- Lambdaはarm64・512MB・タイムアウト120秒・予約済み同時実行数1とする。
- EventBridge Schedulerは1分間隔・Flexible Time Windowなしで有効にする。
- relay専用SGからRDSの5432とSQS専用Interface VPCエンドポイントの443だけを許可する。
- LambdaのDB認証は既存 `vector_app` に対するRDS IAM認証とTLSを使い、DBロール・schemaは変更しない。
- SQSの送信操作はrelay専用ロール・対象4キュー・専用VPCエンドポイントに限定する。
- Lambda・SchedulerのIAMロールとpermissions boundaryは既存ECSのものから独立させる。
- AI鍵・Redis・HTTPプロキシ・アプリ認証用秘密はLambdaへ渡さない。
- CloudWatch Logsは専用グループと既存の保持日数設定を使う。
- relayではX-Rayを採用せず、tracingは `PassThrough` とする。CloudWatch LogsとLambda標準メトリクスで確認し、この関数だけをSemgrepのActive tracing推奨ルールから除外する。X-Ray送信権限は追加しない。

| キューの接尾辞 | 受け付けるイベント | Lambda環境変数 |
|---|---|---|
| `article-completion` | `article.incomplete_recorded` | `SQS_ARTICLE_COMPLETION_QUEUE_URL` |
| `article-curation` | `article.acquired` / `article.completed_to_analyzable` | `SQS_ARTICLE_CURATION_QUEUE_URL` |
| `article-assessment` | `article.curated_signal` | `SQS_ARTICLE_ASSESSMENT_QUEUE_URL` |
| `article-embedding` | `article.assessed_in_scope` | `SQS_ARTICLE_EMBEDDING_QUEUE_URL` |

この表は基盤の送信先定義であり、現行relayが4工程すべてを配信することを意味しない。実行時の選択とpublisherはembedding向けイベントだけを扱う。

実名には `name_prefix` を付ける。`AWS_REGION` はLambdaが提供する予約済み環境変数を使用する。DB用環境変数は `DATABASE_URL`、`DB_IAM_AUTH=true`、`ENV=production` とする。

## 実行と終了の契約

- 設定はDB接続・IAM認証方式・環境・region・4工程のQueue URLを使用し、保守用DB設定やAPI全体の設定を読み込まない。
- leaseは150秒、SQS接続timeoutは3秒、応答待ちは5秒、SDK送信は最大1試行とする。資格情報取得先の通信timeoutとは区別する。
- DBセッションは既存factoryを使い、relayとfailure handlerが同じEngineを共有する。送信中にDB sessionを保持しない。
- DB障害・呼び出し失敗はLambdaへ伝播し、入口で再送しない。Engine終了だけの失敗も正常応答にせず、先行する実行失敗があればそちらを維持する。
- SQSクライアントの終了と診断はpublisherに任せ、入口では通知を重複させない。

## 初回構築

1. bootstrapの既存管理手順に従い、専用boundary・作成可能ロール・CI管理権限を先に適用する。通常applyロールではbootstrapを更新できない。
2. PRをmainへマージし、既存 `AWS terraform apply` のproduction承認を経て本体を適用する。イメージ未指定ではLambdaとscheduleは作成せず、キュー・IAM・SG・エンドポイント・ログ・schedule groupまで作成する。
3. handlerと `awslambdaric` を含むmainのbackendイメージを、既存 `AWS app images` workflowでECRへ配布する。ECSと同一のイメージ成果物を使う。backendイメージは単一のlinux/arm64でビルドする。
4. ECRのbackendリポジトリで、その成果物の `sha256:...` digestを確認する。
5. mainの `AWS terraform apply` を手動起動し、入力 `outbox_relay_image_digest` にdigestを指定する。production承認後、ECRでの存在確認とTerraform planを経てLambdaと有効なscheduleを作成する。初期の無効配置とは異なり、この適用から送信が始まるため、先にConsumer受信とSSM設定を準備する。
6. 以下の開始後の監視手順で配信・保存・失敗を確認する。手動invokeは開始条件とせず、CIのapplyロールに検証用InvokeFunction権限を追加しない。

手順2・5はAWSリソースを作成・変更する操作であり、この実装PRの作成だけでは実行されない。初回作成時にはVPC接続の準備でLambdaがPendingになる場合があるため、Activeへの遷移とSchedulerのENABLEDを確認する。

## 更新とロールバック

通常のPR planと自動applyは `resolve-outbox-relay-image.py` で現行Lambdaのdigestをstateから引き継ぐ。初回にLambdaがない場合だけnullを使用し、state取得失敗や不正な参照をnullへ置き換えない。

生成中の空JSONをTerraformが読まないよう、自動読み込み対象外の一時ファイルに出力し、処理成功後に `.auto.tfvars.json` へ移動する。

更新・ロールバックはいずれも `AWS terraform apply` の手動入力に、ECRに存在するbackend digestを明示する。空欄では現状を保持する。Lambdaのコード配布はTerraformが担当し、ECSイメージのpush・rolloutだけではLambdaを更新しない。

Lambda以外も含む本体スタック全体のplanを行うため、更新時にも差分を確認する。以前のworkflow revisionからの適用は既存の最新infra確認で拒否する。CLIからの独自applyや `update-function-code` を通常の更新経路にしない。

既存ECRの保持世代数はLambdaにも適用される。古いdigestは削除され得るため、ECSだけを更新し続けてLambdaを保持窓外へ取り残さない。ロールバック先はECRで存在を確認する。タグだけの付け替えで永続保持される構成ではない。

## 検証再開時の確認

以下は実装時の検証項目である。ローカル検証と本番検証は区別し、定期送信開始の現在の状態と未実施項目は次節に記録する。

- `/check` によるbackend検証と、追加したLambda handlerのunit・実PostgreSQLテストを実行する。
- 同一プロセスでhandlerを繰り返し呼び出し、DB接続を残さず配信記録を確定し、配信済みイベントを再送しないことを確認する。
- 実DBへの接続失敗が例外になり、設定を戻した次の呼び出しが接続できることを確認する。
- bootstrap・本体のTerraform format check／validate／planを実行し、既存ECSロール・起動方法に変更がないことを確認する。
- `python3 -m unittest discover -s infra/aws/scripts -p 'test_resolve_outbox_relay_image.py'` で、初回null・通常保持・明示ロールバック・不正stateの拒否を確認する。
- イメージのアーキテクチャ・manifest形式と、`awslambdaric` が起動できることを確認する。
- AWS公式Runtime Interface Emulatorを外部からマウントするローカル確認では、コンテナを `--read-only --tmpfs /tmp` で起動し、Lambdaと同じhandlerを指定する。RIEは本番イメージへ追加しない。
- デプロイ後、AI・Redis設定なしでIAM認証によるDB接続が成功し、Schedulerが現在のTerraformの目標状態であることを確認する。

Lambdaの実行ログ・DB接続先・認証情報・Terraform stateを公開PRやartifactに貼り付けない。

## 定期送信の開始と監視（スライス4.2）

2026-09-10、本体apply #44でConsumer受信を有効化し、apply #45でrelayイメージを更新した。relayのActive・LastUpdateStatus=Successfulと、SchedulerのDISABLEDを確認済み。今回の変更ではSchedulerだけを有効化するコードを準備し、AWSへの反映はまだ行っていない。

ユーザーの選択により、Outboxの事前件数確認は省略する。件数は不明として記録し、開始後の実処理を確認する。件数確認用のbastion等は追加しない。イベント発生日時による除外はなく、古い未配信イベントも既存の再試行・lease・配信停止条件を満たせば送信対象になる。

**適用**

1. PR planで既存Schedulerの `state: DISABLED → ENABLED` だけが差分（0追加・1更新・0削除）になることを確認する。relay・Consumerのdigest、Lambda設定、キュー、ECSに不要な差分があれば原因を確認してから適用する。
2. mainへマージし、既存 `AWS terraform apply` のproduction承認を経て適用する。両digestの入力は空欄とし、state上の現在の版を維持する。イメージの再ビルド・公開やbootstrap再適用は不要。
3. AWSの対象アカウント・東京リージョンで `vector-outbox-relay` グループの同名SchedulerがENABLED・rate(1 minute)・Flexible Time Window=OFF、対象が `vector-outbox-relay` Lambdaであることを確認する。ConsumerのSQS受信はEnabled・BatchSize=1・最大同時実行10・ReportBatchItemFailuresを維持する。
4. 適用後の再planで差分がないことを確認する。設定の有効化と実データ処理の成功は別に記録する。

**開始後の確認**

- relayのCloudWatchロググループ `/aws/lambda/vector-outbox-relay` とLambdaのInvocations・Errors・Duration・Throttlesで、定期起動とDB接続の成否を確認する。`outbox_delivery_stopped`・`outbox_failure_recording_failed` と既存の `outbox_publish_configuration_failure` を確認し、設定不備が続けば停止する。
- embeddingキューのNumberOfMessagesSent・NumberOfMessagesReceived・NumberOfMessagesDeleted、ApproximateNumberOfMessagesVisible・NotVisible・AgeOfOldestMessageで送信・受信・滞留を確認する。1回の確保は最大10件だが、再試行や重複起動を含めた厳密な毎分10件の上限ではない。
- `/aws/lambda/vector-embedding-consumer` の `embedding_message_completed`・`embedding_message_failed`・`embedding_initialization_failed`・`embedding_message_input_invalid` を確認する。検証済みevent_id・分析記事IDで処理を関連付け、完了理由が保存か生成済みかも確認する。成功監査・保存結果の確認は既存のDB参照経路が利用できる時点で行い、未確認ならその旨を記録する。
- ConsumerのErrorsだけで部分バッチ応答の失敗を判定しない。処理時間・スロットリング・DLQ件数と通知・RDSのCPU/接続数/空きメモリ等も確認する。再配信・DLQ移動・Taskiq併用は実際に観測した結果を残し、発生していなければ未検証とする。
- キューが空、Lambdaが正常終了、またはログがないという事実だけで全件配信・保存成功としない。送信が観測できなければ、実際の起動・エラーとOutboxの配信可能イベントの有無を切り分ける。本文・APIキー・DB URL・生のTerraform stateを確認記録へ貼らない。

## 緊急停止と再開

SSM・DB・プロキシ・認証の共通障害や設定不備が継続した場合は、まずrelayの新規定期起動を止める。個別記事の失敗は既存の再試行・DLQへ任せる。

1. 管理者でAWSコンソールの対象アカウント・東京リージョンを確認し、EventBridge Schedulerのグループ `vector-outbox-relay` にある同名スケジュールを開く。
2. スケジュールARNのアカウント・リージョンと、ターゲットARNが同じアカウントの `vector-outbox-relay` Lambdaであることを確認する。不一致なら操作しない。
3. スケジュールの無効化操作を実行し、再取得して状態DISABLEDを確認する。スケジュールを削除したり、ターゲット・実行間隔を変更したりしない。
4. 既に配信・実行された呼び出しは残り得る。Consumerはキュー内の処理を続けるため、Consumer側にも継続障害があれば[既存の受信停止手順](README.md#consumerの受信有効化監視停止再開スライス41)で停止する。実行中処理の強制終了・キュー削除・purgeは行わない。
5. 停止中は再有効化するapplyを承認しない。Terraformの同じSchedulerを `state = "DISABLED"` に変更してマージ・反映し、手動変更とコードを一致させる。
6. 原因解消後、Consumerの受信再開を確認したうえで、Terraformを `ENABLED` へ戻し、production承認付きapplyでrelayを再開する。digestは維持する。

[Schedulerの状態管理](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-schedule-state.html)に従い、緊急停止にはコンソールの無効化を使う。CLIの `update-schedule` は省略した設定を既定値へ戻すため、Stateだけを指定するコマンドは使わない。[UpdateSchedule仕様](https://docs.aws.amazon.com/cli/latest/reference/scheduler/update-schedule.html)

実環境での送信・保存・失敗の確認は、この変更のマージと適用後に実施する。1分間隔は到達時間の上限を保証しない。

## 仕様参照

- [Lambdaで通常のPythonイメージを使用する](https://docs.aws.amazon.com/lambda/latest/dg/python-image.html)
- [LambdaのVPC接続とENI権限](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html)
- [SQSのVPCエンドポイント](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-internetwork-traffic-privacy.html)
- [SQLAlchemyの複数イベントループとNullPool](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops)

初期実装時の検証結果（現在の適用状況はスライス4.2参照）: 関連単体テスト47件、Lambda入口とrelayの実DBテスト33件が成功した。変更したPythonのlint・format、outbox_relay.tfのformat check、隔離したTF_DATA_DIRでのinit -backend=false・validateも成功した。validateには既存設定の非推奨警告が残る。ディレクトリ全体のformat checkでは今回未変更のterraform.tfvarsに書式差分があり、変更対象だけを整形・検証した。全テスト、本番適用、AWSへの実送信、Slack到達確認は実施していない。
