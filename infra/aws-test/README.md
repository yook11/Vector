# テスト専用AWS環境

EmbeddingのAWSスモーク試験に必要な設備を、本番と別のアカウントへ作成するTerraform構成。
対象は **ローカル設定に登録したテスト専用アカウント / ap-northeast-1**。Terraform定義と、DB準備・試験・結果回収・自動削除を行う実行コマンドを用意する。
旧構成では実AWSの構築と削除・残存確認を実施済みだが、起動準備で失敗しEmbedding試験は未実行。新しい`aws-smoke-up`・`aws-smoke-prepare`、small化、SSM専用経路の実AWS検証は未実施。`up`・`prepare`とTerraform単体では環境を自動削除しない。

全体の試験契約は [Embedding AWSスモークテスト仕様](../../specs/pipeline/embedding-aws-smoke-test.md) を参照。

## 配置と管理範囲

| 配置 | 管理するもの | state |
|---|---|---|
| `bootstrap/` | state用S3、構築ロールと権限境界、backend/proxy用ECR | このディレクトリのローカルstate |
| `smoke/` | 試験ごとのVPC、EC2、RDS、Lambda、SQS、SSMエンドポイント、実行ロール、ログ | 専用S3の`smoke/<run_id>/terraform.tfstate` |
| `modules/runtime-policy/` | 実行権限の定義（bootstrapの権限上限とsmokeの個別権限で共用） | リソースなし |

本番のTerraformやstate、デフォルトVPCは参照しない。既存のSquid設定テンプレートと非公開IP範囲の定義だけをファイルとして再利用する。
state用S3とその保護設定、ECRと保持設定、構築ロール・管理ポリシー・権限境界は`prevent_destroy`を維持する。S3/ECRの強制削除も無効にする。
構築ロールへのポリシー関連付けには`prevent_destroy`を付けず、関連付けの変更・置換を可能にする。保護はin-placeの権限変更を防ぐものではなく、resourceブロック自体を削除すると失われるため、常設基盤の変更は別途レビューする。
ローカルstate・バックアップ・tfvars・plan・`.terraform/`はGit管理外。bootstrapのstateは暗号化された端末上で保管・バックアップし、紛失しないこと。
両構成の`.terraform.lock.hcl`はGit管理する。

## 使う権限

bootstrapは既存の`WorkloadAdministrator`のSSOプロファイルを使用する。
smokeの作成・変更・削除は`VectorTestManager`のプロファイルから`/vector-test/bootstrap/vector-test-terraform`ロールを引き受ける。
試験投入・結果取得には`VectorTestRunner`を使う。割当方針・インラインポリシー・利用開始前の設定は[アクセス権限](ACCESS.md)を参照する。bootstrapの権限細分化は別途扱う。
信頼先はローカル設定で指定した同一アカウントのSSO権限セット1つに限定する。構築ロールの最大セッション時間、providerとbackendの引受時間を1時間に合わせる。
SSOログイン元の認証が有効な間はSDKが引受認証情報を更新するため、環境全体の処理時間を1時間で打ち切る設定ではない。

構築ロールの書込先は試験用の名前・IAMパス・タグ・state領域へ限定する。構築ロール自身、権限境界、ECR、S3設定を変更する権限は持たせない。
実行ロールの作成時には種類ごとの権限境界が必須で、`PassRole`は試験用のLambda/EC2ロールと対応サービスに限定する。
AWS APIによってリソース指定できないDescribe系やENI管理等には`Resource: "*"`が残る。
試験を投入するSSM Run Commandやログの回収は、実行コマンドがRunnerプロファイルで行う。Terraform構築ロールへ実行権限を混ぜない。

| 実行主体 | 許可 |
|---|---|
| Lambda | 試験SQS受信・削除、専用AIキー取得、対象RDSの`vector_app`接続、専用ログ、LambdaサービスのENI管理 |
| 準備・確認EC2 | SSM管理、テストECR読取、対象RDS管理シークレット取得、`vector`/`vector_app`接続、試験SQS投入・属性取得、専用ログ |
| プロキシEC2 | SSM管理、proxyリポジトリ読取、専用ログ |

**構築ロールは、テストアカウントの設備を管理する信頼された運用者として扱う。RunIdごとの権限隔離ではない。**
通常作成するrunnerのポリシーは対象DB・管理secretだけを許可するが、構築ロールはruntimeポリシーを変更し、EC2へ渡せる。
その上限は同じアカウント・東京リージョン内の全DB IDに対する`vector`/`vector_app`接続と、`rds!db-*`の管理secret読取まで含む。ネットワーク・DB側の許可は別途必要だが、「その実行のDB以外には到達不能」とは扱わない。
本番や他用途のDB・秘密値を置かない専用アカウントの信頼された運用者向けにこの範囲を維持する。将来、第三者への権限委譲や試験ごとの隔離が必要になったら、利用前に権限境界を再設計する。

Lambdaの関数コードからのENI操作は明示的に拒否する。Lambdaに管理シークレットや準備用DBロールへの権限は渡さない。
プロキシにはAIキー・DBへの権限を付与しない。

## 通信と起動

| 送信元 | 直接接続 | HTTPプロキシ経由 |
|---|---|---|
| Lambda（非公開） | RDS:5432、SSM Interface endpoint:443 | Geminiのみ |
| 準備・確認EC2（非公開） | RDS:5432、SSM/ssmmessages Interface endpoint:443、IMDS | ECR、ECRレイヤー用S3、Secrets Manager、SQS、CloudWatch Logs、AL2023パッケージ |
| プロキシEC2（公開IPあり） | SSM endpoint、インターネット:80/443、IMDS | 使用しない |

VPCは`10.80.0.0/16`。Lambda・EC2の用途別にサブネットを分け、DB用には1a/1cの2サブネットを用意する。
公開ルートはプロキシ用だけ。NAT Gateway、IP転送、SSH受信口は作らず、プロキシの3128番はLambdaと実行用EC2からのみ受け入れる。
SSMとssmmessagesのInterface endpointを主AZに各1つ設け、Private DNSを有効にする。SSM Agentはプロキシを経由せず、Dockerやパッケージ取得の成否に依存しない管理通信を使う。
Lambdaとrunnerの許可先は、送信元サブネット別のSquid ACLで分離する。runner用のAWS許可先をLambdaへ流用しない。
プロキシのアクセスログには時刻・HTTPメソッド・ステータスだけを残し、URL・ヘッダーを記録しない。

- AMIはAWS公開パラメーターからAL2023 ARM64を取得し、実際に使ったIDを出力する。
- 両EC2はIMDSv2必須、CPUクレジットStandard、ルートディスク暗号化・終了時削除。両EC2は`t4g.small`、ルートディスクはプロキシ8GB・runner20GB。
- プロキシはDockerを入れ、指定digestの既存Squidイメージを取得し、systemdで起動する。ECR認証情報は取得中のみ一時ディレクトリに保持する。
- runnerはSSM Agentのプロキシ環境変数を解除し、Docker daemonだけにプロキシ設定を用意する。ホスト上の後続コマンドは`/etc/vector-test/proxy.env`を`set -a`で読み込む。ここには接続先だけが入り、秘密値は含めない。
- `no_proxy`には通常のSSM/ssmmessagesホスト名、RDSホスト名、localhost、IMDSを指定する。LambdaのSSMクライアントは本番同様の直接接続を使う。
- Docker内へ環境変数は自動伝播しない。後続の準備コンテナには必要な接続設定を明示し、IMDSv2のhop limit=1を保つため`--network host`で実行する。認証を無効にして回避しない。
- 起動時の外部コマンドは1回300秒・最大5回で打ち切る。runnerのプロキシ待ちは最大60回（1回5秒＋10秒間隔）。Squidのサービス再起動にも回数制限を設ける。
- `/var/lib/vector-test/bootstrap-status.json`の`starting`/`ready`/`failed`で起動設定の結果を残す。`ready`はDB準備完了やAWS接続の合格を意味しない。

RDSはPostgreSQL17、`db.t4g.micro`、Single-AZ、暗号化gp3 20GB。DB名は`vector`、管理者は`vector_master`、IAM認証とTLSを必須にする。
LambdaはARM64、1024MB、120秒。接続設定は本番同様に`ENV=production`・IAM認証・TLS検証を使い、接続先だけをテスト用にする。
SQSはStandard、暗号化、保持4日、可視性720秒、バッチ1件、待機0秒、部分失敗応答、最大同時実行2。
アカウント全体の同時実行枠10を踏まえ、Lambdaの予約同時実行は設定しない。
キューは空で作成し、DB準備が終わるまではイベントを投入しない。

## 秘密値とイメージの準備

AIキーはテストアカウントのParameter Storeで、以下の設定で別途登録する。コンソールの値入力欄へ直接入力し、チャット・シェル引数・Terraform変数へ貼らない。

| 設定 | 値 |
|---|---|
| 名前 | `/vector-test/embedding-consumer/gemini-api-key` |
| 階層 | Standard |
| タイプ | SecureString |
| 暗号化キー | AWS管理キー`alias/aws/ssm` |
| 内容 | テスト専用Geminiキー |
| 保持 | 試験ごとのdestroyでは残す。廃止時に別途削除 |

TerraformはAIキーのパラメーター自体も値も作成・取得しない。名前と取得権限だけを定義する。
RDS管理者のパスワードはRDS管理のSecrets Managerへ保存し、TerraformはARNのみ参照する。秘密値のデータソースや出力は置かない。
準備処理は秘密値をメモリー内で扱い、コマンド出力・ログ・レポートへ残さない。

backendとproxyは、対象revisionのARM64イメージをテストECRの`vector-test/backend`と`vector-test/proxy`へ格納してからdigestを指定する。
既存成果物と同じ内容をコピーし、テスト実行中は本番ECRを参照しない。タグは変更不可、タグなしは1日、タグ付きは最新3イメージを保持する。
進行中の試験が参照するdigestを保持数から押し出さないよう、試験中に大量のイメージを登録しない。
ソースrevisionとdigestの対応・ARM64イメージの実行可否は成果物配布工程で確認する。TerraformはECR内のdigest存在を参照するが、ソースとの一致を証明しない。

## 試験ログとセキュリティ検査の例外

試験用RDSのPostgreSQLログは、同じテストアカウントの`/aws/rds/instance/vector-test-<run_id>/postgresql`へ転送する。
ロググループはsmokeのTerraformで先に作成し、7日保持・必須タグを設定する。RDSの削除後にロググループも削除し、本番のログ設定は変更しない。
構築ロールの追加権限は試験RDSのロググループ管理だけとし、LambdaやEC2の書込権限は追加しない。
`execution.log_groups`と`resources.log_groups`の`database`に回収・削除確認用の名前を出力する。
実行コマンドが削除前に4つのロググループを手元へ回収し、取得失敗はグループごとに記録する。
転送先の命名は[AWSのPostgreSQLログ転送仕様](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_LogAccess.Concepts.PostgreSQL.html)に従う。

Semgrepはテスト環境にも適用し、次の2件だけ対象リソースのコメントでルールを限定して除外する。

- RDSのバックアップ保持推奨：試験データは再生成し、削除後の復旧を目的としないため保持0日・最終スナップショットなしを維持する。
- LambdaのX-Ray Active tracing推奨：実行ログとDBの保存結果で試験を確認し、`PassThrough`を明示する。X-Rayの送信権限は追加しない。

## 今回行う静的検証

以下はAWSバックエンドへ接続せず、設備も作らない。Terraform `>= 1.11`、AWS provider `~> 6.0`を使用する。

```sh
terraform -chdir=infra/aws-test/bootstrap init -backend=false -input=false
terraform -chdir=infra/aws-test/smoke init -backend=false -input=false
terraform -chdir=infra/aws-test/bootstrap validate
terraform -chdir=infra/aws-test/smoke validate
terraform fmt -check -recursive infra/aws-test
```

`smoke/tests/*.tftest.hcl`のモックAWS providerを使うplanテストと、起動コマンドの単体テストで、AWSへの変更なしに検証する。

```sh
terraform -chdir=infra/aws-test/smoke test
backend/.venv/bin/python -m unittest discover -s infra/aws-test/scripts/tests -v
```
検証対象はアカウント入力とSSO信頼先の整合、state保護、SGの既存／作成時IAM条件、全設備の必須タグ、IAMパス・権限境界、ログ名と許可ARN、ENI待機の依存関係、通信・SQS・保持設定。
provider/backendのアカウント制限や実際のIAM評価、プロキシ疎通、EC2起動成功はモックの合格だけでは保証できない。

## ローカルのアカウント設定

実アカウントID、bootstrap用の管理者プロファイル、構築ロールの引受元はGit管理外の`.local/account.json`で編集する。
`aws_profile`はbootstrap用管理者、`smoke_aws_profile`はManagerプロファイル、`trusted_admin_role_arn_pattern`はManagerのSSOロールを指定する。サンプルの信頼先名は採用した権限セット名へ置き換える。
Manager／RunnerのプロファイルはAWS CLIへ別途登録し、smoke操作には以下のコマンドでManagerを明示する。
公開するサンプル・構成テストには架空のIDとプロファイルを使い、認証情報そのものはAWS CLIのSSO管理に任せる。

```sh
mkdir -p infra/aws-test/.local
# 初回のみ実行し、既存のローカル設定を上書きしない。
cp -n infra/aws-test/account.example.json infra/aws-test/.local/account.json
```

サンプルの4項目を、確認済みの値へ変更する。信頼先ARNの末尾`*`はSSO割当のsuffixだけを表し、別アカウントや任意の権限セットを許可するワイルドカードにはできない。
次のコマンドはAWSに接続せず、同じ設定元から3ファイルを生成する。

```sh
python3 infra/aws-test/scripts/configure-local.py
```

| 生成ファイル | 用途 |
|---|---|
| `.local/bootstrap.tfvars.json` | bootstrapのアカウント、プロファイル、SSO信頼先 |
| `.local/smoke.tfvars.json` | smokeのアカウント、プロファイル |
| `.local/smoke.tfbackend` | S3バケット、引受ロール、プロファイル、許可アカウント |

`.local/`全体をGit管理から除外する。生成ファイルは直接編集せず、`account.json`を修正して再生成する。
生成スクリプトはbootstrapへ`aws_profile`、smokeのproviderとbackendへ`smoke_aws_profile`を出力する。既存の3項目の設定には`smoke_aws_profile`を追加してから再生成する。
providerは`allowed_account_ids = [var.expected_account_id]`を維持する。backendは未設定時に実在しないIDだけを許可し、生成ファイルを渡し忘れた接続を拒否する。
実行中の環境がある間は接続設定を変更しない。将来の別アカウント移行は、全試験の削除とbootstrap stateの扱いを別途決めてから行う。

## 後で構築plan・削除planを確認する手順

以下は常設基盤の作成とイメージ配布、自動削除・結果回収を実装した後の操作手順。**この工程ではapplyを実行しない。**
リポジトリルートから実行する。各SSOプロファイルを準備し、smokeのplan前に構築ロールのManagerへの信頼先更新を完了する。

```sh
unset TF_DATA_DIR
export AWS_PROFILE="$(python3 -c 'import json; print(json.load(open("infra/aws-test/.local/account.json"))["aws_profile"])')"
aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity --profile "$AWS_PROFILE"
```

返されたAccountが`.local/account.json`の`expected_account_id`と一致することを確認する。

```sh
terraform -chdir=infra/aws-test/bootstrap init -input=false
terraform -chdir=infra/aws-test/bootstrap plan \
  -var-file=../.local/bootstrap.tfvars.json -out=bootstrap.tfplan
terraform -chdir=infra/aws-test/bootstrap show bootstrap.tfplan
```

bootstrapはローカルstateを継続利用する。S3への自動移行や本番stateのコピーは行わない。
bootstrap適用後の出力でstateバケット・ロール・ECRを照合し、次節のイメージ配布が完了してからsmokeのplanへ進む。

試験ごとの非秘密入力は`.local/<run_id>.tfvars`へ保存する。

```hcl
run_id                = "20260911-01"
backend_image_digest  = "sha256:<backendの64桁digest>"
proxy_image_digest    = "sha256:<proxyの64桁digest>"
source_revision       = "<40桁commit SHA>"
gemini_parameter_path = "/vector-test/embedding-consumer/gemini-api-key"
```

実行IDを入力・stateキー・作業データで一致させる。生成したbackend設定を必ず渡し、workspaceは`default`固定とする。
ローカル設定からManagerプロファイル名を読み取り、ログイン結果のAccountとArnがテストアカウントの`VectorTestManager`であることを確認する。

```sh
export TEST_MANAGER_PROFILE="$(python3 -c 'import json; print(json.load(open("infra/aws-test/.local/account.json"))["smoke_aws_profile"])')"
export AWS_PROFILE="$TEST_MANAGER_PROFILE"
aws sso login --profile "$TEST_MANAGER_PROFILE"
aws sts get-caller-identity --profile "$TEST_MANAGER_PROFILE"
```

```sh
export TF_DATA_DIR="$PWD/infra/aws-test/smoke/.terraform/20260911-01"
terraform -chdir=infra/aws-test/smoke init -reconfigure -input=false \
  -backend-config=../.local/smoke.tfbackend \
  -backend-config='key=smoke/20260911-01/terraform.tfstate'
terraform -chdir=infra/aws-test/smoke plan \
  -var-file=../.local/smoke.tfvars.json \
  -var-file=../.local/20260911-01.tfvars \
  -out=smoke.tfplan
terraform -chdir=infra/aws-test/smoke show smoke.tfplan
```

`init -reconfigure`で別実行のstateを移動しない。`-migrate-state`で他試験からstateを移さない。
planではテストアカウント、`Lifecycle=smoke`、`RunId`、常設基盤への変更がないことを確認する。
bootstrap未作成・digest未登録の段階ではsmokeの実planは完了できない。

削除前に、出力と試験結果を回収する。

```sh
terraform -chdir=infra/aws-test/smoke output -json > infra/aws-test/.local/20260911-01-outputs.json
terraform -chdir=infra/aws-test/smoke plan -destroy \
  -var-file=../.local/smoke.tfvars.json \
  -var-file=../.local/20260911-01.tfvars \
  -out=destroy.tfplan
terraform -chdir=infra/aws-test/smoke show destroy.tfplan
```

上記は削除候補を確認するだけで、削除は実行しない。stateキー・入力・出力の実行IDが一致することを確認する。
実際のdestroyはENI待機スクリプトを使うため、実行元にPython3とAWS CLI v2、および有効なSSO認証が必要。
ENI待機はstateに保存した`aws_profile`でAWS CLIを直接呼ぶため、新しい環境の作成時からManagerを指定する。既存の環境に対してプロファイルだけを切り替えず、保存済みの接続設定と削除方法を先に確認する。

## イメージのビルド・配布担当

配布担当は**VectorTestManager**。構築ロールにはECR push権限を追加しない。
本番向けGitHub Actionsは本番用ロール・ECRに接続するため、この試験用配布には使用しない。将来CI化する場合はテストアカウント専用の配布権限を用意する。
以下は配布担当が後で実行する手順であり、この変更ではbuild・pushを行わない。

1. 対象commitのクリーンなcheckoutを用意する。ローカルテストの合否と対応revisionを記録する。
2. そのcheckoutから、既存DockerfileでARM64イメージを作る。テスト向けにコードを書き換えず、本番へ出す候補成果物として扱う。
3. Managerプロファイルでログインし、bootstrapのECRへ一意なタグでpushする。イメージを後から本番へ配布するときも、同じ成果物をコピーする。
4. ECRからdigestを取得し、対応するcommit SHAと一緒に試験入力へ保存する。

クリーンなcheckoutのルートで、Managerプロファイルを明示して実行する。プロファイル名はローカルの登録名に置き換える。

```sh
set -euo pipefail
export AWS_PROFILE=vector-test-manager
aws sso login --profile "$AWS_PROFILE"
export AWS_REGION=ap-northeast-1
EXPECTED_TEST_ACCOUNT_ID="$(python3 -c 'import json; print(json.load(open("infra/aws-test/.local/account.json"))["expected_account_id"])')"
TEST_ACCOUNT_ID="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)"
if [ "$TEST_ACCOUNT_ID" != "$EXPECTED_TEST_ACCOUNT_ID" ]; then
  echo "接続先アカウントが一致しません" >&2
  exit 1
fi
TEST_REGISTRY="$TEST_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
SOURCE_REVISION="$(git rev-parse HEAD)"
IMAGE_TAG="$SOURCE_REVISION"  # 既存タグは上書きせず、登録済みdigestを再利用する。
aws ecr get-login-password --profile "$AWS_PROFILE" --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "$TEST_REGISTRY"
docker build --platform linux/arm64 --provenance=false \
  -t "$TEST_REGISTRY/vector-test/backend:$IMAGE_TAG" backend
docker build --platform linux/arm64 --provenance=false \
  -t "$TEST_REGISTRY/vector-test/proxy:$IMAGE_TAG" infra/squid
docker push "$TEST_REGISTRY/vector-test/backend:$IMAGE_TAG"
docker push "$TEST_REGISTRY/vector-test/proxy:$IMAGE_TAG"
aws ecr describe-images --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --repository-name vector-test/backend --image-ids "imageTag=$IMAGE_TAG" \
  --query 'imageDetails[0].imageDigest' --output text
aws ecr describe-images --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --repository-name vector-test/proxy --image-ids "imageTag=$IMAGE_TAG" \
  --query 'imageDetails[0].imageDigest' --output text
docker logout "$TEST_REGISTRY"
```

既に本番候補が登録済みなら**再ビルドせず**、配布担当がそのARM64イメージを元ECRからdigest指定でpullし、テストECRへtag/pushして両ECRのdigest一致を確認する。
元ECRの読取には別途認められた読取プロファイルを使う。コピー時以外、試験アカウントから本番リソースへ接続しない。

## 削除と後続コマンド

smokeのstateでEC2・ルートEBS・公開IP・SSM endpoint・VPC・ログ・IAM実行ロールを管理する。
RDSは削除保護なし・最終スナップショットなし・自動バックアップ保持0日。RDS管理シークレットはDB削除に伴い削除されるため、削除確認ではARNも追跡する。
削除順は「Lambda関数 → ENI消滅待機 → 実行ポリシー・ロール・ネットワーク」とする。`terraform_data.lambda_eni_cleanup`のdestroy処理が対象subnet/SGのENIを50分上限で照会する。
待機中はLambdaの実行権限を保持し、ENI削除自体はLambdaに任せる。構築ロールにdetach/delete権限は追加しない。
接続先不一致・認証失敗・応答不正・期限切れは待機失敗とし、IAM権限の削除を止める。同じstate・入力でdestroyを再試行する。
この待機を含むstateを新規作成し、固定した入力で全体destroyする運用を対象とする。試験中のsubnet・SG・IAMロールの置換やアカウント変更は行わず、全体削除後に別runとして作り直す。
既存stateにこの待機を追加する場合は通常applyによる作成が先に必要。cleanupのtaint、resourceブロック削除、`create_before_destroy`では待機が省略され得るため使用しない。
AWSが生成するENIには試験タグが付かない場合があるため、残存確認は記録したVPC IDでも検索する。
EC2の自動割当公開IPはEIPではなく、インスタンス終了で解放される。EBSは出力のボリュームIDも使って確認する。
削除が失敗したらstateと入力を保持し、同じ実行IDで再試行する。Terraformのdestroy成功だけで残存確認済みとは扱わない。

以下のコマンドをMakefileと`scripts/aws-smoke.py`に実装している。
構築・削除・設備の残存確認はManagerから構築ロールを引き受け、試験投入・結果取得はRunnerを使う。ENI削除待機の照会はManagerで直接行う。各工程のプロファイルを明示し、Runnerへ設備管理権限を追加してまとめない。

| コマンド | 責務 |
|---|---|
| `make aws-smoke-up RUN_ID=...` | 環境作成→EC2・SSM・プロキシ確認。成功・失敗時とも環境を保持 |
| `make aws-smoke-prepare RUN_ID=...` | 起動済み環境の準備ファイル生成→イメージ取得→DB準備・確認。環境を保持 |
| `make aws-smoke-test RUN_ID=... TEST=aws_tests/embedding/` | 準備済み環境で指定対象を実行→結果回収。環境を保持 |
| `make aws-smoke TEST=aws_tests/embedding/` | 構築→DB準備→指定対象の試験→結果回収→削除→残存確認 |
| `make aws-smoke-destroy RUN_ID=...` | 対象実行だけの削除・再試行・残存確認 |
| `make aws-smoke-status RUN_ID=...` | 保存済み結果と最新の残存状況の確認 |

試験期限はpytest起動からsetup・call・teardown全体で共有し、`TIMEOUT`で正の整数秒を指定する（既定300秒）。AWS操作前の対象収集は別途60秒、構築・準備・回収・削除にも別の有限期限を設ける。
試験成功と削除成功を別々に記録し、未実行・確認不能を成功にしない。実行元の電源断等に備える独立したAWS側の削除監視は、今回の範囲には含めない。

## 起動確認だけを実行する

`aws-smoke-up`は既存smoke構成全体（RDS・Lambda・SQSを含む）を作成するが、認証schema生成・DB初期化・イベント投入・AI呼出を行わない。Terraform、AWS CLI、botocoreを含む`backend/.venv`、Manager/RunnerのSSO認証、既存のアカウント設定とECR digestを用意する。手元のDockerは`up`には不要で、`prepare`と一括試験の認証schema生成で使用する。

```sh
make aws-smoke-up RUN_ID=20260912-up01
# 作成完了済みの環境は再applyせず、同じIDで起動・疎通を再確認する。
make aws-smoke-up RUN_ID=20260912-up01
# 確認後は明示的に削除し、残存も確認する。
make aws-smoke-destroy RUN_ID=20260912-up01
make aws-smoke-status RUN_ID=20260912-up01
```

`RUN_ID`を省略すると毎回新しいIDを生成する。既存環境の再確認には表示されたIDを指定する。Runnerプロファイルを変更する初回操作は`aws-smoke.py up --runner-profile <名前>`を使い、再確認は保存済みプロファイルを使用する。

成功条件は、固定した入力と実リソース・Lambda digestの整合、両EC2のステータスチェック、両SSM AgentのOnline、runnerへの今回固有の短い応答、runnerのbootstrap readyとDocker稼働、プロキシ3128番への接続、プロキシ経由のECR BatchGetImageで指定proxy digestを取得できること。ECRの空結果・失敗応答も不合格とし、GeminiやDBの正常動作まで確認したとは扱わない。

起動確認のSSMコマンドはCloudWatch転送を無効にし、短い結果をRun Commandから直接取得する。プロキシ障害時も管理通信の成否を確認できる。構築後の起動確認は全項目で900秒を共有し、各SSMコマンドは実行30秒・配信60秒、AWS通信にも短い期限を設定する。待機中は30秒間隔で工程・経過時間を表示する。処理中のAPI通信とプロセス終了の時間が上限に加わる場合がある。

| 再実行時の状態 | 動作 |
|---|---|
| AWS構築前の失敗 | 保存済み入力・所有者を照合して再試行 |
| Terraform apply完了、後続の確認が失敗または成功 | 再applyせず、実リソースと起動・疎通を確認 |
| apply途中の失敗・中断、完了が不明 | 自動再applyせず、削除後に新しいIDで作り直すよう案内 |
| 削除開始済み・削除済み | 起動を拒否し、新しいIDを案内 |
| 入力改変・所有者不一致・同じIDの並行操作 | 拒否 |

最新結果は`result.json`と`summary.txt`、各回の結果は`up-attempts/<連番>/result.json`に残す。失敗時はログ回収も期限付きで試み、回収失敗によって起動失敗の原因を隠さない。DB準備・試験は`not_run`のままにし、`up`の合格判定へ含めない。

**`up`は失敗・Ctrl-C・SIGTERMでも環境を自動削除しない。** 表示された削除コマンドで後片付けする。`aws-smoke`は新しいIDで始める一括試験のままで、`up`の続きからDB準備へ進むには`aws-smoke-prepare`を使う。準備後は`aws-smoke-test`で対象を指定して試験する。現行`status`は残存確認用で、稼働中の設備があると非0で終了し、同じIDの操作中はロックで拒否する。

## 起動済み環境のDB準備を実行する

`aws-smoke-prepare`は構築と`up`の成功が記録された既存RUN_IDを必須とする。保存済み設定・実行主体・所有者・実リソースを照合し、現在の起動・SSM・プロキシ疎通を再確認してから準備へ進む。手元のDockerと、保存されたsource revisionを参照できるGit checkoutも必要。

```sh
make aws-smoke-up RUN_ID=20260912-prepare01
make aws-smoke-prepare RUN_ID=20260912-prepare01
# 準備済みならDDLを再適用せず、現在のDB状態を確認する。
make aws-smoke-prepare RUN_ID=20260912-prepare01
make aws-smoke-destroy RUN_ID=20260912-prepare01
make aws-smoke-status RUN_ID=20260912-prepare01
```

準備は`auth_schema`（ファイル生成）、`image`（固定backend digest取得）、`database`（DB準備・確認）の工程に分ける。完成したDDL・初期設定SQLは`prepared-assets/`へ保存し、`manifest.json`のrevision・ハッシュ一致時だけ再利用する。失敗途中の作業は`assets-failed-*/`へ残し、そのRUN_ID専用の一時コンテナを回収して再生成する。取得済みの同じイメージも再利用する。

空のDBには既存の初期設定SQL、認証DDL、イメージ内のAlembicを順に適用する。DB内の実revisionとイメージのheadが一致し、pgvector、認証テーブル、カテゴリ・ニュースソース初期データが存在し、`vector_app`のIAM認証・TLS接続でEmbedding対象テーブルを読み取れることを成功条件とする。記事作成・SQS投入・AI呼出は行わない。

| 再実行時のDB状態 | 動作 |
|---|---|
| 空、または初期ロール・拡張の準備だけ完了 | 正本から初期化する |
| 同じmigration headで必要な状態を確認できる | DDLを再適用せず、既存データを保持して成功 |
| 認証DDLだけ存在・revision不一致・必要なテーブルやデータが不足 | 失敗理由を記録し、削除後に新しいRUN_IDでの再作成を案内 |
| 別のDB準備が継続中 | 専用の非待機advisory lockで拒否し、処理終了後に再試行 |

準備ロックはDB準備全体に保持し、既存migrationロックとは異なるキーを使う。[PostgreSQLのsession advisory lock](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)により、手元の操作が中断してもDB側で継続する準備との重複を防ぐ。通常のRUN_ID単位のローカル操作ロックも維持する。

**`prepare`は成功・失敗・Ctrl-C・SIGTERMとも環境を保持する。** Terraform applyは行わず、削除開始済み環境や未成功の`up`からは進めない。最新の工程結果・安全な失敗理由・SSM Command IDは`result.json`へ、各回の結果は`prepare-attempts/<連番>/result.json`へ保存する。再実行時に前回のDB成功結果を引き継いで合格にはしない。後片付けには表示された`aws-smoke-destroy`を使う。

この変更のローカル検証は次だけに限定する。DB fixtureは実行専用コンテナ・DBを作成し、終了時に削除する。

```sh
backend/.venv/bin/python -m unittest discover -s infra/aws-test/scripts/tests -p test_smoke_prepare.py -v
(cd backend && .venv/bin/python -m pytest local_tests/test_aws_smoke_prepare_database.py -q)
(cd infra/aws-test/scripts/tests && ../../../../backend/.venv/bin/python -m unittest \
  test_smoke_up.RunTests.test_up_only_provisions_and_checks \
  test_smoke_up.RunTests.test_one_shot_keeps_schema_before_apply_and_cleanup_after_database -v)
```

これに変更したPythonファイルのRuff・format確認と`git diff --check`を加える。無関係なテスト一式やTerraformテストは回さない。ローカルDBは既存fixtureのPostgreSQL 18とパスワード認証を使うため、RDS PostgreSQL 17上のIAM認証・TLS・SSM経路は後続の実AWS確認が必要。

## 準備済み環境で対象を指定して試験する

```sh
make aws-smoke-test RUN_ID=20260912-up01 TEST=aws_tests/embedding/
make aws-smoke-test RUN_ID=20260912-up01 \
  TEST=aws_tests/embedding/test_event_processing.py::test_redelivery_keeps_saved_embedding
make aws-smoke-test RUN_ID=20260912-up01 TEST=aws_tests/embedding/ TIMEOUT=600
# 試験コードを修正後も、同じRUN_IDで上のコマンドを再実行できる。
make aws-smoke-destroy RUN_ID=20260912-up01
```

`TEST`は`backend`を基準とした`aws_tests`配下のディレクトリ・ファイル・pytest node IDを1つ指定する。省略、存在しない対象、範囲外のパス、pytestオプションは拒否する。パラメータ化されたnode IDなどシェルの特殊文字を含む値は引用符で囲む。コピーしたコードを60秒以内で収集し、0件・収集エラー・収集時skipがあればAWS操作へ進まない。収集時にAWS操作を行うコードはテスト側に書かず、接続は実行時fixtureで行う。

構築・`up`・`prepare`の成功が記録された既存RUN_IDを必須とする。保存済み入力・所有者・実行主体・実リソースを照合し、起動・SSM・プロキシ疎通を再確認する。削除開始済み・未準備・同じRUN_IDへの並行操作は拒否する。Terraform apply、DB再初期化、イメージ変更は行わないため、対象が必要とする設備は既存環境に用意されていることが前提となる。

実行時点の`backend/aws_tests`を未コミットの修正・追加も含めて`test-attempts/<連番>/code/`へコピーし、そのコピーを実行する。テスト・conftest・補助コード・fixtureファイルを含み、`.env`などの隠しファイル、キャッシュ、ログ、範囲外へ向くシンボリックリンクは取り込まない。テスト用fixtureはこの範囲内に置く。実行開始後の編集は次回から反映される。環境・アプリイメージのrevisionと保存済み定義はRUN_IDに固定したまま、テストコードのGit revisionとファイルハッシュを別に記録する。

1件以上の収集対象すべてがsetup・call・teardownを完了して成功し、pytest終了コードが0の場合だけ試験合格とする。件数は固定せず、skip・xfail・未開始・未完了を合格に数えない。ケース状態は逐次JSONへ保存し、中断でJUnitが完成しなくてもJSON・pytestログ・コードを残す。CloudWatchログは成功・失敗とも約2分を上限に各回の`logs/`へ回収し、回収失敗はケース結果と分けて記録してコマンド全体を非0で終了する。

**個別実行は成功・失敗・Ctrl-C・SIGTERMでも環境を保持する。** 表示された再実行・削除コマンドを使う。`TIMEOUT`はpytestとEmbedding runtimeで同じ期限を共有する。期限切れ・中断時は既存のプロセス終了処理を使い、終了待ちに最大60秒が加わる場合がある。投入済みSSM処理やLambdaが即時停止したとは扱わず、再実行時も前回投入分が続いている可能性を考慮する。

実行制御を変更した場合の関連検証は次に限定する。実AWS・実AI・DB・Terraform・無関係なbackend/frontend一式は実行しない。

```sh
backend/.venv/bin/python -m unittest discover -s infra/aws-test/scripts/tests -p test_smoke_test.py -v
(cd infra/aws-test/scripts/tests && ../../../../backend/.venv/bin/python -m unittest \
  test_smoke_up.RunTests.test_up_only_provisions_and_checks \
  test_smoke_up.RunTests.test_one_shot_keeps_schema_before_apply_and_cleanup_after_database \
  test_smoke_prepare.PrepareTests.test_prepare_keeps_environment_and_archives_retry_failure \
  test_smoke_up.CommandTests.test_execution_timeout_still_interrupts_subprocess -v)
```

これに変更PythonファイルのRuff・format確認と`git diff --check`を加える。AWSのIAM・SSM・CloudWatchと実Embedding処理の確認は後続のAWS実行で行う。

## 実行コマンドの使い方

実装対象の問題は、構築・DB準備・試験・回収・削除を手作業でつなぐと、失敗途中の設備や結果を取りこぼすこと。既存Terraform出力、`infra/aws/db-provision.sql`、対象revisionのBetter Auth CLI、backendイメージ内のAlembic、実行時点の`backend/aws_tests`から指定したテストを使う。
実行ID・接続アカウント・イメージ・stateを固定し、試験の成否と削除の成否を分けて残す。本番設定・migration・IAMの変更、新しい試験ケース、AWS側の独立した期限監視は今回の対象に含めない。
新しい起動経路は静的検証・モックテストで確認し、実AWSでの起動・再確認・削除は別の受入確認で判断する。

リポジトリルートで、次の準備を完了する。

- Terraform、AWS CLI、Docker、既存の`backend/.venv`（botocore・pytestを含む）を用意する。
- `.local/account.json`の`smoke_aws_profile`をManagerへ設定し、Managerと`vector-test-runner`でSSOログインする。`aws_profile`はbootstrap管理者のままにする。
- `.local/images.tfvars.json`に登録済みの`source_revision`・`backend_image_digest`・`proxy_image_digest`を保存し、対象commitをローカルGitで参照できるようにする。
- テスト専用SSMキーを登録する。コマンドはキーを作成・上書きしない。

```sh
make aws-smoke TEST=aws_tests/embedding/
# 実行IDを指定する場合（使用済みIDは再利用しない）
make aws-smoke RUN_ID=20260912-01 TEST=aws_tests/embedding/

# 中断・失敗後の再試行
make aws-smoke-destroy RUN_ID=20260912-01
make aws-smoke-status RUN_ID=20260912-01
```

上の最初の2行は代替の実行方法であり、続けて実行する必要はない。`aws-smoke`は実際にAWS設備を作成し、Gemini通信を行う。Runnerのプロファイル名を変える場合は`backend/.venv/bin/python infra/aws-test/scripts/aws-smoke.py run --test aws_tests/embedding/ --runner-profile <名前>`を使う。

1. 指定対象をコピーして収集を確認した後、接続先・SSOロール・ECR digestを確認し、専用S3の`smoke/<run_id>/owner.json`を条件付きで作成する。同じIDを別の端末から同時に使うことも拒否する。この所有記録はstateとともに残す。
2. 対象revisionのfrontendを既存Dockerfileのdevelopment stageでビルドする。ポートを公開しない内部ネットワーク上の一時PostgreSQL 17でBetter Auth CLIを実行し、auth schemaのDDLを生成する。一時コンテナ・匿名ボリューム・ネットワークは終了時に削除し、ローカルのビルドキャッシュは保持する。
3. 実行専用ディレクトリへ固定したTerraform定義・入力でcreate planを保存し、既存managed resourceを含まないことを確認して適用する。
4. runnerのSSMとbootstrap完了を待ち、Lambdaと同じbackend digestをpullする。準備用ファイルを一時マウントして、既存のRDS初期化SQL、生成済みauth schema、イメージ内のAlembic `upgrade head`を適用する。管理シークレットはrunner内のメモリーだけで扱い、schema適用以降は`vector`のIAM認証と証明書・ホスト名検証を使う。
5. DB準備後に指定対象のpytestを起動し、`TIMEOUT`（既定300秒）以内に実行する。成功・失敗が確定すれば早めに終了する。
6. JUnit・工程結果・CloudWatchログを保存し、固定入力による全体destroyを行う。stateが空であることと、VPC内ENI・EC2/EBS・RDS/バックアップ/管理シークレット・SQS・Lambda/トリガー・IAM実行ロール・ログ等の残存を照合する。

一括実行`aws-smoke`は作成開始以降の失敗・通常のCtrl-C・SIGTERMでも回収後に削除を試みる。回収失敗でも削除は進め、失敗を結果へ残す。構築applyは45分、起動・SSM・プロキシ確認は合計15分、イメージpullは5分、DB準備コマンドは11分、ログ回収は約2分、destroy applyは90分、残存の再照会は3分を上限とする。通信・プロセス終了の待機時間は別途加わり得る。削除の90分には既存のENI消滅待機（最大50分）を含む。

電源断・ネットワーク断・強制終了・SSO期限切れでは自動削除が完了しない場合がある。端末を復旧し、必要なら同じSSOセッションへ再ログインして`aws-smoke-destroy`を実行する。削除中の二度目の中断でも完了は保証しない。stateロックの強制解除や`-target`による部分削除は行わない。

## 試験環境を片付ける

```sh
make aws-smoke-destroy RUN_ID=20260912-up01
# 途中失敗・Ctrl-C・SIGTERMの後も同じコマンドで再試行する。
make aws-smoke-destroy RUN_ID=20260912-up01
```

構築applyを開始した既存RUN_IDを指定する。構築途中・DB準備失敗・試験失敗でも削除でき、`up`や`prepare`の成功は要求しない。構築未開始の場合は`this_command_has_not_started_provisioning`で拒否し、削除完了とは記録しない。

保存済み定義・入力のハッシュ、アカウントとManagerの実行主体、所有記録、試験専用bucketと`smoke/<RUN_ID>/terraform.tfstate`を確認してから処理を開始する。不一致と同じRUN_IDへの並行操作を拒否する。起動時に保存したTerraform定義・入力で全体destroyを行い、常設IAM・ECR・state基盤・SSM設定は保持する。ローカルの固定設定、コード、試験結果、過去の削除履歴も削除しない。

Runnerの実行主体を確認して最新のCloudWatchログを回収する。Runner認証・回収が失敗してもManagerによる削除を続け、`deletion_status=passed`と`status=failed`を分けて記録して非0終了する。不存在のロググループは`absent`と記録し、権限不足・通信失敗・期限切れと区別する。

| 状態 | 動作 |
|---|---|
| stateに対象設備がある | 削除plan・apply後にstateとAWS残存を確認 |
| stateが空、AWS残存もなし | destroy applyを再実行せず削除確認を完了 |
| stateが空、AWS残存あり | 期限付きで再照会。残存すれば非0終了し、state外の設備を直接削除しない |
| 削除前後の照会が確認不能 | 削除完了にしない。削除前照会の失敗でも対象確認済みのdestroyは試みる |

**削除完了は、stateが空で、既知IDと試験タグ等によるAWS照会でも残存なしを確認できた場合だけ記録する。** 過去に記録したVPC・EC2/EBS・管理シークレット・Lambdaトリガー等のIDは、空の照会結果で失わないように保持する。削除済み環境の再確認では過去の回収失敗を引き継がず、今回のログ回収・削除確認結果で終了コードを決める。

各回を`destroy-attempts/<連番>/`へ記録し、工程の状態・時刻・安全な失敗理由、ログ回収、Terraformログ・plan、削除前後の照会結果を保持する。最新結果と参照先を`result.json`・`summary.txt`へ反映し、試験合否と過去の各回のファイルは変更しない。旧形式の実行記録からも削除できる。

ログ回収120秒、destroy apply90分、残存再照会180秒など既存の期限を維持する。工程と経過時間、取得できる残存件数を表示し、通信・プロセス終了待ちの時間が追加される場合がある。中断時は途中結果と同じ再実行コマンドを残す。ローカル中断でAWS側の削除や投入済みSSM・Lambdaが即時停止したとは扱わない。

一括実行もこの削除処理を使う。同じ一括実行内で回収したログは`test-attempts`側の場所を削除履歴に関連付け、重複回収しない。準備・試験・回収の失敗後も削除を試み、いずれかの失敗を終了コード0で隠さない。

削除処理の変更時は、次の専用テストと影響する既存ケース、変更PythonのRuff・format確認、`git diff --check`に検証を限定する。

```sh
backend/.venv/bin/python -m unittest discover -s infra/aws-test/scripts/tests -p test_smoke_destroy.py -v
(cd infra/aws-test/scripts/tests && ../../../../backend/.venv/bin/python -m unittest \
  test_smoke_up.RunTests.test_old_report_can_still_be_destroyed \
  test_smoke_up.RunTests.test_destroy_collects_new_logs_after_up \
  test_smoke_up.RunTests.test_one_shot_keeps_schema_before_apply_and_cleanup_after_database \
  test_smoke_up.RunTests.test_resources_are_read_back_and_mapping_is_known_before_inventory \
  test_smoke_up.RunTests.test_missing_message_endpoint_and_changed_lambda_image_fail \
  test_smoke_test.ControlTests.test_one_shot_selection_failure_never_starts_aws_and_runtime_failure_cleans_up \
  test_smoke_test.ControlTests.test_log_collection_failure_is_separate_and_keeps_environment -v)
```

実AWS・実AI・DB・Terraform・backend/frontend全体のテストは実行しない。実際の削除時間、IAM権限、ENI解放、AWS残存確認は後続のAWS実行で確認する。

## 削除後に確認するファイル

`.local/runs/<run_id>/`へ、所有者のみ読み書きできる権限で保存する。Gitへ追加しない。

| ファイル | 確認できる内容 |
|---|---|
| `result.json` / `summary.txt` | 工程ごとの未実行・実行中・合否、時刻、失敗種別、試験結果、SSM Command ID |
| `test-attempts/<連番>/result.json` | 対象・収集node ID・期限・時刻・終了コード・回収状況 |
| `test-attempts/<連番>/code/` / `source.json` | 実行コードとテスト側Git revision・ファイルハッシュ |
| `test-attempts/<連番>/test-results.json` / `junit.xml` / `pytest.log` | 各ケースの途中結果・成否、記事・イベント・SQSメッセージの識別情報 |
| `test-attempts/<連番>/collection*.json` / `collection.log` / `execution-collection.json` | 事前収集と本実行の収集結果・pytest終了コード |
| `test-attempts/<連番>/logs/` | 各回に関連付けたCloudWatchログと回収状況 |
| `logs-*/` | Lambda・runner・proxy・RDSのJSON Linesと、グループごとの回収状況 |
| `inventory.json` / `remaining.json` | 累積した既知の識別情報と、最新の残存照会結果 |
| `destroy-attempts/<連番>/result.json` | 削除完了とコマンド全体の合否、工程・時刻・失敗理由、回収先 |
| `destroy-attempts/<連番>/logs/` | 手動削除前のCloudWatchログと回収状況（一括実行は試験側ログを参照） |
| `destroy-attempts/<連番>/known-resources.json` / `inventory.json` / `remaining.json` | 各回の既知ID、削除前と削除後の照会結果 |
| `destroy-attempts/<連番>/*.log` / `destroy.tfplan` / `state-*.json` / `state-*.txt` | 各回のTerraformログ・削除plan・stateの確認結果 |
| `outputs.json` / `inputs.tfvars.json` | 接続先・イメージdigest・source revision等の固定入力と出力（秘密値なし） |
| `workspace/` / `manifest.json` / `aws.config` | 削除に再利用する定義、ハッシュ、SSOメタデータ（トークンなし） |
| `create*.log` / `destroy*.log` | Terraformの構築結果と旧形式の削除結果 |

ケース別JSONはpytestのsetup・call・teardownの報告ごとに更新する。未開始は`not_run`、実行中は`running`、完了後は`passed`・`failed`・`skipped`等を記録する。中断して完了していないケースは成功にしない。最新結果は既存サマリーへ反映し、過去の`test-attempts`は上書きしない。
保存済み定義や入力のハッシュが変わっていれば削除を止める。`.local/runs/<run_id>`は削除確認が終わるまで移動・編集・削除しない。`status`は最初に保存済みサマリーを表示してからAWSを照会するため、認証できなくても前回の結果は読める。照会不能は「残存なし」にしない。

`aws-smoke`は試験・回収・削除・照会のどれかが失敗すると終了コード1を返す。手動の`destroy`は今回のログ回収と削除確認、`status`は最新の残存確認の成否を終了コードで返し、過去の試験結果は変更しない。CloudWatchへまだ到着していない診断ログまで全量回収できた保証ではなく、試験の合否は対応する完了記録とDB結果で判定する。

## 費用の目安

事前に取得した東京リージョンのオンデマンド単価に基づき、small 2台として再計算した。請求額の保証ではなく、利用開始時に再確認する。

| 計算リソース | 時間単価（USD） |
|---|---:|
| RDS PostgreSQL `db.t4g.micro` | 0.0250 |
| proxy EC2 `t4g.small` | 0.0216 |
| runner EC2 `t4g.small` | 0.0216 |
| **計算料金の合計** | **約0.068/時** |

これとは別に、RDS/EBSストレージ、公開IPv4、SSM Interface endpointの稼働・通信、Secrets Manager、Lambda/SQS/CloudWatch、外向きデータ転送、AI APIの料金がかかる。
RDS・EC2等の最低課金時間や作成・準備・削除待ちがあるため、**試験5分を設備全体の課金時間としない**。
常設IAMロールそのものには稼働料金はなく、SSM Standard Parameterは標準スループットでパラメーター保持の追加料金なし。ECRとstate用S3には容量・リクエスト料金がかかる。RDS管理シークレットは別途課金対象。

- [EC2オンデマンド料金](https://aws.amazon.com/ec2/pricing/on-demand/)
- [RDS PostgreSQL料金](https://aws.amazon.com/rds/postgresql/pricing/)
- [VPC料金](https://aws.amazon.com/vpc/pricing/)
- [Systems Manager料金](https://aws.amazon.com/systems-manager/pricing/)
- [Secrets Manager料金](https://aws.amazon.com/secrets-manager/pricing/)

## 実装時の参照

- [S3 backendのロック・アカウント制限](https://developer.hashicorp.com/terraform/language/backend/s3)
- [EC2のリソース・タグ条件](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html)
- [Lambdaイベントソースマッピングのタグ権限](https://docs.aws.amazon.com/lambda/latest/dg/tags-esm.html)
- [RDS管理シークレットのライフサイクル](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-secrets-manager.html)
- [SSM Agentのプロキシ設定](https://docs.aws.amazon.com/systems-manager/latest/userguide/configure-proxy-ssm-agent.html)
- [Docker daemonのプロキシ設定](https://docs.docker.com/engine/daemon/proxy/)
- [ECRレイヤーのS3通信](https://docs.aws.amazon.com/AmazonECR/latest/userguide/vpc-endpoints.html)
- [Run CommandのCloudWatch出力](https://docs.aws.amazon.com/systems-manager/latest/userguide/sysman-rc-setting-up-cwlogs.html)
- [Squidログ形式](https://www.squid-cache.org/Doc/config/logformat/)
