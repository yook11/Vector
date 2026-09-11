# Embedding AWSスモークテスト

Status: 方針合意・実装前

## Problem

ローカルで確認したEmbedding処理を、本番へ出すイメージでAWS上でも動かし、実SQS配信・Lambda起動・IAM認証・SSM取得・外部AI通信・RDS保存の接続を確認する。
本番とは別のAWSアカウントに必要時だけ試験環境を構築し、結果を回収して削除する。

## Evidence

- `backend/local_tests/embedding/`: migration適用済みDBを使った保存・失敗・接続解放・重複配送・同時処理のテスト。
- `backend/local_tests/database.py`: ロール・Better Auth・Alembicを用いたDB準備の既存実装。
- `backend/app/lambda_handlers/embedding/`: 実ハンドラー、設定、IAM接続とSSM取得。
- `infra/aws/embedding_consumer.tf`: 本番のLambda・IAM・SQSイベントソースマッピング。
- `infra/aws/outbox_relay.tf`: キューと再配送設定。
- `infra/aws/proxy.tf`と`infra/aws/templates/squid.conf.tftpl`: 本番の外向きプロキシ構成。
- `infra/aws/scripts/verify-embedding-runtime.py`: リソースを差し替えるイメージ内起動試験であり、実AWS接続の保証ではない。

既存Terraformは構成の参考とし、本番のstateや実行リソースをテストから操作しない。
追加済みのローカルテストにはユーザー指示による未実行分があり、追加したことだけでローカル合格とは扱わない。

## Invariants

- テスト専用AWSアカウントを使用し、本番のDB・キュー・Lambda・ネットワーク・IAM実行ロール・SSM・AIキー・ログを共有しない。
- Terraformの構成とstateを本番から分離し、構築・試験・削除の開始前に実行先アカウントIDを照合する。
- 本番へ出すものと同一内容のイメージをテスト用ECRへコピーし、タグだけでなくdigestとソースrevisionを記録する；テスト向けの製品コード差し替えはしない。
- DB構造・ロールは既存の正本から適用し、試験を通すための追加GRANT、IAM認証の迂回、`create_all`や`stamp`による代替は行わない。
- 本番の実行権限の意図を保ち、リソース参照先をテストアカウントに限定する。
- Lambdaは実アプリ用DBロールを使用し、準備用の管理権限を持たせない。
- 外部AIは実通信し、記事データとキーはテスト専用のものを使う。
- `.env`を利用せず、秘密値をTerraform変数・state・出力・試験レポートへ書かない。
- 必須の確認不能・期限切れは合格にせず、試験失敗と環境削除失敗を別々に記録する。

## 構成

| リソース | 用途・方針 |
|---|---|
| Lambda | 実Embeddingハンドラーを検証対象のイメージで起動する |
| SQS・イベントソースマッピング | 実配信でLambdaを起動する；初期値は本番同様1回1件・ReportBatchItemFailures |
| RDS PostgreSQL | 小規模なSingle-AZ構成、外部公開なし、IAM DB認証有効 |
| VPC・サブネット・セキュリティグループ | LambdaとRDSをプライベート側へ置き、DB接続元を必要なリソースに限定する |
| プロキシEC2 | 外部AIへの通信経路；NAT Gatewayは作成せず、テスト内部からの利用に限定する |
| テスト実行用EC2 | DB準備・イベント投入・処理待機・保存結果の照合・結果出力を担当する |
| IAM | Lambda・プロキシ・テスト実行用EC2の責務に応じた専用ロールを用意する |
| SSM Parameter Store | テスト専用AIキーなどを格納し、本番の値は参照しない |
| CloudWatch Logs | Lambdaとテスト実行の詳細ログを確認する |
| テスト用ECR | 同一内容の検証対象イメージを保持する |

RDSはSingle-AZでも、DBサブネットグループ等のAWS要件を満たすサブネットを用意する。
DBの容量・冗長化・同時実行規模は本番と同一にする必要はない。
イメージのコピーは配布工程だけで行い、試験実行中に本番リソースへのアクセスを必要としない。

### 通信経路

- LambdaからRDS、テスト実行用EC2からRDSはVPC内で接続する。
- 外部AIへのHTTPS通信はテスト専用プロキシを経由する。
- プロキシは外向き接続が可能な側に配置し、外部から利用可能な公開プロキシにしない。
- LambdaのSSM取得、EC2の管理、SQS投入、ECR取得、ログ出力に必要なAWSサービスへの経路も定義する。
- アカウント分離だけでは通信経路は成立しない；SDKとSSM Agentのプロキシ設定、必要な許可先、VPCエンドポイントの要否を実装前に確定する。
- Lambdaをパブリックサブネットへ置くだけで外部通信できる構成とは扱わない。

SSM AgentにはHTTPプロキシ設定があるが、製品SDKが同じ設定を使うとは仮定しない。
既存実装にはプロキシ設定を明示的に無効化するAWSクライアントもあるため、各クライアントと通信先を照合する。

## テスト実行用EC2の責務

1. テストRDSへ必要な初期ロール・拡張・Better Auth管理スキーマ・Alembic migrationを既存の正本に従って適用する。
2. Alembic headへの到達と、試験に必要な初期データ・アプリロールの成立を確認する。
3. 実行IDで識別できる生成可能な分析済み記事を作る。
4. 実イベント契約に従うメッセージをテストSQSへ投入する。
5. 期限付きで対象イベントの完了を待ち、別DB接続から保存結果を確認する。
6. 各確認の合否と失敗理由を出力し、実行元へ結果を返す。

準備用の管理接続と結果確認用の接続を区別し、管理接続の成功をLambdaのアプリ権限での保存成功の代替にしない。
EC2の操作はSSM Run Commandを第一候補とし、結果取得は手元またはCIから行う。
Terraformはインフラを管理し、migration・データ準備・試験の実行は専用スクリプトで行う。

## 初回に実装する試験

正常なイベント1件を実SQSへ投入し、以下をすべて満たすことを合格条件とする。

- 検証対象digestのLambdaが、投入したイベントを処理したことを実行ID・イベントID・分析記事IDで関連付けられる。
- 処理が保存成功として完了し、初期化失敗やメッセージ処理失敗になっていない。
- 別DB接続で対象記事のEmbeddingが確定保存され、製品契約の次元数・有限値を満たす。
- 試験専用キューの滞留が期限内に解消する；キューの概算件数だけでは保存成功と判定しない。
- 必要な結果を実行元へ回収できる。

実AIの応答値を固定ベクトルとは比較しない。
接続が成立したことだけでなく、アプリ用DBロールで確定保存されたことまで確認する。

### 後続の試験

- 重複配送: 同じイベントを再投入し、生成済みとして正常終了して保存結果を変更しない。
- 失敗時の配信: 専用DLQとredrive設定を用意し、失敗イベントの再配送と受信上限後のDLQ移動を確認する。
- 可視性タイムアウトと待機期限の関係を定義し、短縮する場合は本番との差分を試験結果に残す。

同時処理・ロールバック・接続解放・エラー分類の細かなケースをAWSで再網羅しない。
Outbox relayからの送信は初回の範囲に含めず、SQSへの投入を試験入口とする。

## 試験結果と削除

S3は初回の結果保存先として必須にしない。

| 内容 | 保存先 |
|---|---|
| 合否、確認項目、失敗理由、実行時刻、アカウント・リージョン、実行ID、イベント・記事ID、revision・digest・migration revision | 手元のレポート、またはCI成果物 |
| Lambda・テスト実行の詳細ログ | テストアカウントのCloudWatch Logs；削除前に必要分を実行元へ回収する |
| 長期保管レポート | 必要になった段階でS3を検討する |

結果回収は成功・失敗のどちらでも試み、その後に試験環境を削除する。
結果取得に失敗しても課金リソースを無期限に残さず、取得失敗と削除結果を実行元に記録する。
RDS・EC2・プロキシ・関連ストレージ・IP・キュー・ネットワーク等の残存を確認し、削除失敗を見える形で返す。
テストDBのデータ保存は目的ではないため、通常の試験終了時はDBスナップショットを残さない。

AWSアカウント・アクセス設定・Terraform stateの基盤は試験ごとの削除対象から分離する。
ECRイメージ・SSMの秘密値・ログの保持期間と削除単位は、実装時に明示する。
Terraform stateの保存先と試験結果の保存先は別の責務であり、結果用S3を作らないことはstate用S3の利用を禁止する意味ではない。

## Non-goals

- 本番環境の複製、本番アカウントや本番データを使う試験。
- 全工程・負荷・性能・高可用性・障害復旧の網羅。
- 常設のRDS、NAT Gateway、本番と同規模のネットワーク設備。
- 初回からのDLQ試験、Outbox relay・Schedulerの構築、長期保管用S3。
- この仕様書作成に伴うAWSアカウント作成、インフラ適用、テスト実行。

## Done

実装の完了条件は、専用アカウントで構築・DB準備・正常系試験・結果回収・削除を一連の手順で実施でき、各工程の失敗を識別できることとする。
削除後に再構築して同じ手順が成立し、残存リソースの扱いが明確であることを確認する。
次工程へ進む条件は「対象revisionのローカルテスト合格 → 同一イメージのAWS試験合格」とし、未実行を合格と記録しない。
現時点ではユーザーの指示によりテストは実行しない。

## 着手順序

1. ユーザーがテスト専用AWSアカウントを作成する。
2. アカウントID、使用リージョン、作業用ロール・SSO等のアクセス方法を確認する；秘密キーは会話に貼らない。
3. テスト専用Terraformの配置・state、通信経路、イメージ配布、秘密値の登録・保持方法を確定する。
4. 基盤とテスト実行用EC2を実装し、DB準備から正常系試験・結果回収・削除までを接続する。
5. テスト実行が許可された段階でローカル・AWSの順に検証する。
6. 重複配送、失敗時の再配送・DLQ試験を追加する。

## 参照

- [VPC接続したLambdaの制約](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html)
- [RDSへのIAM認証接続](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.Connecting.html)
- [SQSイベントソースの失敗処理](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
- [SSM AgentのHTTPプロキシ設定](https://docs.aws.amazon.com/systems-manager/latest/userguide/configure-proxy-ssm-agent.html)
- [Run CommandのCloudWatch Logs出力](https://docs.aws.amazon.com/systems-manager/latest/userguide/sysman-rc-setting-up-cwlogs.html)
