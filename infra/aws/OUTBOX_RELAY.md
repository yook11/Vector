# Outbox relay Lambda基盤

## 現在の実装範囲

relayは既存maintenance workerへ同居させず、専用Lambdaで実行する。共通backendコンテナイメージを利用し、Terraformで起動コマンドだけを切り替える。Dockerfileと既存ECSの起動方法・権限は変更しない。

現在のhandlerは `SELECT 1` によるDB接続確認専用である。正常応答は次の形式とし、イベント配信成功を意味しない。

```json
{"check": "database_connectivity", "status": "ok"}
```

Outboxの確保・更新、SQS送信、consumer Lambda、Taskiqからの処理移行は未実装である。Schedulerは `DISABLED` に固定して作成し、この段階では有効化しない。

## リソースと境界

- 同一アカウント・リージョンに工程別のStandardキューを4つ作成する。
- 全キューをSSE-SQSで暗号化し、メッセージ保持期間を14日とする。
- Lambdaはarm64・512MB・タイムアウト30秒・予約済み同時実行数1とする。
- EventBridge Schedulerは1分間隔・Flexible Time Windowなしで定義するが、無効のままとする。
- relay専用SGからRDSの5432とSQS専用Interface VPCエンドポイントの443だけを許可する。
- LambdaのDB認証は既存 `vector_app` に対するRDS IAM認証とTLSを使い、DBロール・schemaは変更しない。
- SQSの送信操作はrelay専用ロール・対象4キュー・専用VPCエンドポイントに限定する。
- Lambda・SchedulerのIAMロールとpermissions boundaryは既存ECSのものから独立させる。
- AI鍵・Redis・HTTPプロキシ・アプリ認証用秘密はLambdaへ渡さない。
- CloudWatch Logsは専用グループと既存の保持日数設定を使う。
- 接続確認段階ではX-Rayを採用せず、tracingは `PassThrough` とする。CloudWatch LogsとLambda標準メトリクスで確認し、この関数だけをSemgrepのActive tracing推奨ルールから除外する。X-Ray送信権限は追加しない。

| キューの接尾辞 | 受け付けるイベント | Lambda環境変数 |
|---|---|---|
| `article-completion` | `article.incomplete_recorded` | `SQS_ARTICLE_COMPLETION_QUEUE_URL` |
| `article-curation` | `article.acquired` / `article.completed_to_analyzable` | `SQS_ARTICLE_CURATION_QUEUE_URL` |
| `article-assessment` | `article.curated_signal` | `SQS_ARTICLE_ASSESSMENT_QUEUE_URL` |
| `article-embedding` | `article.assessed_in_scope` | `SQS_ARTICLE_EMBEDDING_QUEUE_URL` |

実名には `name_prefix` を付ける。`AWS_REGION` はLambdaが提供する予約済み環境変数を使用する。DB用環境変数は `DATABASE_URL`、`DB_IAM_AUTH=true`、`ENV=production` とする。

## 初回構築

1. bootstrapの既存管理手順に従い、専用boundary・作成可能ロール・CI管理権限を先に適用する。通常applyロールではbootstrapを更新できない。
2. PRをmainへマージし、既存 `AWS terraform apply` のproduction承認を経て本体を適用する。イメージ未指定ではLambdaとscheduleは作成せず、キュー・IAM・SG・エンドポイント・ログ・schedule groupまで作成する。
3. handlerと `awslambdaric` を含むmainのbackendイメージを、既存 `AWS app images` workflowでECRへ配布する。ECSと同一のイメージ成果物を使う。backendイメージは単一のlinux/arm64でビルドする。
4. ECRのbackendリポジトリで、その成果物の `sha256:...` digestを確認する。
5. mainの `AWS terraform apply` を手動起動し、入力 `outbox_relay_image_digest` にdigestを指定する。production承認後、ECRでの存在確認とTerraform planを経てLambdaと無効scheduleを作成する。
6. 検証再開・デプロイ承認後、対象Lambdaへの起動権限を持つ運用者がコンソールから `{}` を入力して手動起動する。上記の接続確認応答と専用ログを確認する。CIのapplyロールには検証のためのInvokeFunction権限を追加しない。

手順2・5はAWSリソースを作成・変更する操作であり、この実装PRの作成だけでは実行されない。初回作成時にはVPC接続の準備でLambdaがPendingになる場合があるため、Activeを確認してから起動する。

## 更新とロールバック

通常のPR planと自動applyは `resolve-outbox-relay-image.py` で現行Lambdaのdigestをstateから引き継ぐ。初回にLambdaがない場合だけnullを使用し、state取得失敗や不正な参照をnullへ置き換えない。

生成中の空JSONをTerraformが読まないよう、自動読み込み対象外の一時ファイルに出力し、処理成功後に `.auto.tfvars.json` へ移動する。

更新・ロールバックはいずれも `AWS terraform apply` の手動入力に、ECRに存在するbackend digestを明示する。空欄では現状を保持する。Lambdaのコード配布はTerraformが担当し、ECSイメージのpush・rolloutだけではLambdaを更新しない。

Lambda以外も含む本体スタック全体のplanを行うため、更新時にも差分を確認する。以前のworkflow revisionからの適用は既存の最新infra確認で拒否する。CLIからの独自applyや `update-function-code` を通常の更新経路にしない。

既存ECRの保持世代数はLambdaにも適用される。古いdigestは削除され得るため、ECSだけを更新し続けてLambdaを保持窓外へ取り残さない。ロールバック先はECRで存在を確認する。タグだけの付け替えで永続保持される構成ではない。

## 検証再開時の確認

現在はユーザー指示により検証を保留している。実行済みと扱わないこと。

- `/check` によるbackend検証と、追加したLambda handlerのunit・実PostgreSQLテストを実行する。
- 同一プロセスでhandlerを繰り返し呼び出し、DB接続を残さずOutboxの全列が不変であることを確認する。
- 実DBへの接続失敗が例外になり、設定を戻した次の呼び出しが接続できることを確認する。
- bootstrap・本体のTerraform format check／validate／planを実行し、既存ECSロール・起動方法に変更がないことを確認する。
- `python3 -m unittest discover -s infra/aws/scripts -p 'test_resolve_outbox_relay_image.py'` で、初回null・通常保持・明示ロールバック・不正stateの拒否を確認する。
- イメージのアーキテクチャ・manifest形式と、`awslambdaric` が起動できることを確認する。
- AWS公式Runtime Interface Emulatorを外部からマウントするローカル確認では、コンテナを `--read-only --tmpfs /tmp` で起動し、Lambdaと同じhandlerを指定する。RIEは本番イメージへ追加しない。
- デプロイ後、AI・Redis設定なしでIAM認証によるDB接続が成功し、Schedulerが無効であることを確認する。

Lambdaの実行ログ・DB接続先・認証情報・Terraform stateを公開PRやartifactに貼り付けない。

## 次のタスク

送信契約、送信失敗の分類、再試行と停止判断、処理件数・実行時間・lease期間を個別に定義し、接続確認handlerをrelay本体の呼び出しへ置き換える。

初回有効化前に蓄積したOutboxイベントを削除・停止・送信のどれで扱うか決定する。1分間隔は到達時間の上限保証ではなく、即時性より費用を優先する選択とする。LambdaとSchedulerの呼び出し回数だけで無料と断定せず、実行時間・VPCエンドポイント・SQS・ログの費用も含める。

## 仕様参照

- [Lambdaで通常のPythonイメージを使用する](https://docs.aws.amazon.com/lambda/latest/dg/python-image.html)
- [LambdaのVPC接続とENI権限](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html)
- [SQSのVPCエンドポイント](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-internetwork-traffic-privacy.html)
- [SQLAlchemyの複数イベントループとNullPool](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops)
