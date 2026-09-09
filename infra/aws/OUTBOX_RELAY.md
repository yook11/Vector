# Outbox relay Lambda基盤

## 現在の実装範囲

relayは既存maintenance workerへ同居させず、専用Lambdaで実行する。共通backendコンテナイメージを利用し、Terraformで起動コマンドだけを切り替える。Dockerfileと既存ECSの起動方法・権限は変更しない。

handlerは起動ごとに設定・DB Engine・publisher・failure handlerを組み立て、OutboxRelay.run_onceを1回実行してEngineを終了する。正常応答は今回の実行完了を意味し、全件の送信成功やconsumerの処理完了を意味しない。

```json
{"status": "completed"}
```

ベクトル生成向けイベントの停止最大100件・確保最大10件・SQSバッチ送信最大1回と、イベントごとの結果記録を接続済みである。consumer Lambda、Taskiqからの処理移行は対象外である。Schedulerは `DISABLED` に固定して作成し、この段階では有効化しない。

## リソースと境界

- 同一アカウント・リージョンに工程別のStandardキューを4つ作成する。
- 全キューをSSE-SQSで暗号化し、メッセージ保持期間を14日とする。
- Lambdaはarm64・512MB・タイムアウト120秒・予約済み同時実行数1とする。
- EventBridge Schedulerは1分間隔・Flexible Time Windowなしで定義するが、無効のままとする。
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
5. mainの `AWS terraform apply` を手動起動し、入力 `outbox_relay_image_digest` にdigestを指定する。production承認後、ECRでの存在確認とTerraform planを経てLambdaと無効scheduleを作成する。
6. 検証再開・デプロイ承認後、対象Lambdaへの起動権限を持つ運用者がコンソールから `{}` を入力して手動起動する。この起動は実際にOutboxを更新しSQSへ送信するため、事前に古いイベントと既存パイプラインの重複処理対策を確認する。完了応答・配信状態・専用ログを確認する。CIのapplyロールには検証のためのInvokeFunction権限を追加しない。

手順2・5はAWSリソースを作成・変更する操作であり、この実装PRの作成だけでは実行されない。初回作成時にはVPC接続の準備でLambdaがPendingになる場合があるため、Activeを確認してから起動する。

## 更新とロールバック

通常のPR planと自動applyは `resolve-outbox-relay-image.py` で現行Lambdaのdigestをstateから引き継ぐ。初回にLambdaがない場合だけnullを使用し、state取得失敗や不正な参照をnullへ置き換えない。

生成中の空JSONをTerraformが読まないよう、自動読み込み対象外の一時ファイルに出力し、処理成功後に `.auto.tfvars.json` へ移動する。

更新・ロールバックはいずれも `AWS terraform apply` の手動入力に、ECRに存在するbackend digestを明示する。空欄では現状を保持する。Lambdaのコード配布はTerraformが担当し、ECSイメージのpush・rolloutだけではLambdaを更新しない。

Lambda以外も含む本体スタック全体のplanを行うため、更新時にも差分を確認する。以前のworkflow revisionからの適用は既存の最新infra確認で拒否する。CLIからの独自applyや `update-function-code` を通常の更新経路にしない。

既存ECRの保持世代数はLambdaにも適用される。古いdigestは削除され得るため、ECSだけを更新し続けてLambdaを保持窓外へ取り残さない。ロールバック先はECRで存在を確認する。タグだけの付け替えで永続保持される構成ではない。

## 検証再開時の確認

ローカル検証と本番検証は区別する。本番適用・定期起動の有効化・AWSへの実送信・Slack到達確認は未実施である。

- `/check` によるbackend検証と、追加したLambda handlerのunit・実PostgreSQLテストを実行する。
- 同一プロセスでhandlerを繰り返し呼び出し、DB接続を残さず配信記録を確定し、配信済みイベントを再送しないことを確認する。
- 実DBへの接続失敗が例外になり、設定を戻した次の呼び出しが接続できることを確認する。
- bootstrap・本体のTerraform format check／validate／planを実行し、既存ECSロール・起動方法に変更がないことを確認する。
- `python3 -m unittest discover -s infra/aws/scripts -p 'test_resolve_outbox_relay_image.py'` で、初回null・通常保持・明示ロールバック・不正stateの拒否を確認する。
- イメージのアーキテクチャ・manifest形式と、`awslambdaric` が起動できることを確認する。
- AWS公式Runtime Interface Emulatorを外部からマウントするローカル確認では、コンテナを `--read-only --tmpfs /tmp` で起動し、Lambdaと同じhandlerを指定する。RIEは本番イメージへ追加しない。
- デプロイ後、AI・Redis設定なしでIAM認証によるDB接続が成功し、Schedulerが無効であることを確認する。

Lambdaの実行ログ・DB接続先・認証情報・Terraform stateを公開PRやartifactに貼り付けない。

## 次のタスク

Lambda入口へのrelay接続は実装済みである。次はイメージ配布・本番適用・有効化前の重複処理対策の確認と、実環境での送信・監視確認を行う。

イベント発生日時による除外は設けず、古いイベントも送信対象に含む。1分間隔は到達時間の上限保証ではなく、即時性より費用を優先する選択とする。LambdaとSchedulerの呼び出し回数だけで無料と断定せず、実行時間・VPCエンドポイント・SQS・ログの費用も含める。

## 仕様参照

- [Lambdaで通常のPythonイメージを使用する](https://docs.aws.amazon.com/lambda/latest/dg/python-image.html)
- [LambdaのVPC接続とENI権限](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html)
- [SQSのVPCエンドポイント](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-internetwork-traffic-privacy.html)
- [SQLAlchemyの複数イベントループとNullPool](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops)

検証結果: 関連単体テスト47件、Lambda入口とrelayの実DBテスト33件が成功した。変更したPythonのlint・format、outbox_relay.tfのformat check、隔離したTF_DATA_DIRでのinit -backend=false・validateも成功した。validateには既存設定の非推奨警告が残る。ディレクトリ全体のformat checkでは今回未変更のterraform.tfvarsに書式差分があり、変更対象だけを整形・検証した。全テスト、本番適用、AWSへの実送信、Slack到達確認は実施していない。
