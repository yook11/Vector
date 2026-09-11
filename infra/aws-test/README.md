# テスト専用AWS環境

EmbeddingのAWSスモーク試験に必要な設備を、本番と別のアカウントへ作成するTerraform構成。
対象は **ローカル設定に登録したテスト専用アカウント / ap-northeast-1**。今回実装するのは設備の定義とEC2起動設定まで。
**自動削除・結果回収の実装が完了するまでは、試験環境をapplyしない。** Terraformだけでは5分後の削除は行われない。

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

bootstrapはローカル設定のSSOプロファイルを使用する。
smokeは同プロファイルから`/vector-test/bootstrap/vector-test-terraform`ロールを引き受ける。
信頼先はローカル設定で指定した同一アカウントのSSO権限セット1つに限定する。構築ロールの最大セッション時間、providerとbackendの引受時間を1時間に合わせる。
SSOログイン元の認証が有効な間はSDKが引受認証情報を更新するため、環境全体の処理時間を1時間で打ち切る設定ではない。

構築ロールの書込先は試験用の名前・IAMパス・タグ・state領域へ限定する。構築ロール自身、権限境界、ECR、S3設定を変更する権限は持たせない。
実行ロールの作成時には種類ごとの権限境界が必須で、`PassRole`は試験用のLambda/EC2ロールと対応サービスに限定する。
AWS APIによってリソース指定できないDescribe系やENI管理等には`Resource: "*"`が残る。
試験を投入するSSM Run Commandやログの回収は、次工程の実行元がローカル設定のテスト管理者プロファイルで行う想定で、Terraform構築ロールへ実行権限を混ぜない。

| 実行主体 | 許可 |
|---|---|
| Lambda | 試験SQS受信・削除、専用AIキー取得、対象RDSの`vector_app`接続、専用ログ、LambdaサービスのENI管理 |
| 準備・確認EC2 | SSM管理、テストECR読取、対象RDS管理シークレット取得、`vector`/`vector_app`接続、試験SQS投入・属性取得、専用ログ |
| プロキシEC2 | SSM管理、proxyリポジトリ読取、専用ログ |

**構築ロールは、テストアカウントの設備を管理する信頼された運用者として扱う。RunIdごとの権限隔離ではない。**
通常作成するrunnerのポリシーは対象DB・管理secretだけを許可するが、構築ロールはruntimeポリシーを変更し、EC2へ渡せる。
その上限は同じアカウント・東京リージョン内の全DB IDに対する`vector`/`vector_app`接続と、`rds!db-*`の管理secret読取まで含む。ネットワーク・DB側の許可は別途必要だが、「その実行のDB以外には到達不能」とは扱わない。
本番や他用途のDB・秘密値を置かない専用アカウントの管理者向けにこの範囲を維持する。将来、第三者への権限委譲や試験ごとの隔離が必要になったら、利用前に権限境界を再設計する。

Lambdaの関数コードからのENI操作は明示的に拒否する。Lambdaに管理シークレットや準備用DBロールへの権限は渡さない。
プロキシにはAIキー・DBへの権限を付与しない。

## 通信と起動

| 送信元 | 直接接続 | HTTPプロキシ経由 |
|---|---|---|
| Lambda（非公開） | RDS:5432、SSM Interface endpoint:443 | Geminiのみ |
| 準備・確認EC2（非公開） | RDS:5432、SSM Interface endpoint:443、IMDS | SSM管理チャネル、ECR、ECRレイヤー用S3、Secrets Manager、SQS、CloudWatch Logs、AL2023パッケージ |
| プロキシEC2（公開IPあり） | SSM endpoint、インターネット:80/443、IMDS | 使用しない |

VPCは`10.80.0.0/16`。Lambda・EC2の用途別にサブネットを分け、DB用には1a/1cの2サブネットを用意する。
公開ルートはプロキシ用だけ。NAT Gateway、IP転送、SSH受信口は作らず、プロキシの3128番はLambdaと実行用EC2からのみ受け入れる。
SSM endpointは主AZに1つでPrivate DNSを有効にする。`ssmmessages`はrunnerのプロキシ許可先に含める。
Lambdaとrunnerの許可先は、送信元サブネット別のSquid ACLで分離する。runner用のAWS許可先をLambdaへ流用しない。
プロキシのアクセスログには時刻・HTTPメソッド・ステータスだけを残し、URL・ヘッダーを記録しない。

- AMIはAWS公開パラメーターからAL2023 ARM64を取得し、実際に使ったIDを出力する。
- 両EC2はIMDSv2必須、CPUクレジットStandard、ルートディスク暗号化・終了時削除。プロキシは`t4g.nano`/8GB、runnerは`t4g.small`/20GB。
- プロキシはDockerを入れ、指定digestの既存Squidイメージを取得し、systemdで起動する。ECR認証情報は取得中のみ一時ディレクトリに保持する。
- runnerはSSM AgentとDocker daemonのプロキシ設定を用意する。ホスト上の後続コマンドは`/etc/vector-test/proxy.env`を`set -a`で読み込む。ここには接続先だけが入り、秘密値は含めない。
- `no_proxy`には通常のSSMホスト名、RDSホスト名、localhost、IMDSを指定する。LambdaのSSMクライアントは本番同様の直接接続を使う。
- Docker内へ環境変数は自動伝播しない。後続の準備コンテナには必要な接続設定を明示し、IMDSv2のhop limit=1を保つため`--network host`で実行する。認証を無効にして回避しない。
- 起動時の外部コマンドは1回300秒・最大5回で打ち切る。runnerのプロキシ待ちは最大60回（1回5秒＋10秒間隔）。Squidのサービス再起動にも回数制限を設ける。
- `/var/lib/vector-test/bootstrap-status.json`の`starting`/`ready`/`failed`で起動設定の結果を残す。`ready`はDB準備完了やAWS接続の合格を意味しない。

RDSはPostgreSQL17、`db.t4g.micro`、Single-AZ、暗号化gp3 20GB。DB名は`vector`、管理者は`vector_master`、IAM認証とTLSを必須にする。
LambdaはARM64、1024MB、120秒。接続設定は本番同様に`ENV=production`・IAM認証・TLS検証を使い、接続先だけをテスト用にする。
SQSはStandard、暗号化、保持4日、可視性720秒、バッチ1件、待機0秒、部分失敗応答、最大同時実行2。
アカウント全体の同時実行枠10を踏まえ、Lambdaの予約同時実行は設定しない。
キューは空で作成し、DB準備が終わるまではイベントを投入しない。

## 秘密値とイメージの準備（次工程の前提）

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
次工程の準備処理は秘密値をメモリー内で扱い、コマンド出力・ログ・レポートへ残さない。

backendとproxyは、対象revisionのARM64イメージをテストECRの`vector-test/backend`と`vector-test/proxy`へ格納してからdigestを指定する。
既存成果物と同じ内容をコピーし、テスト実行中は本番ECRを参照しない。タグは変更不可、タグなしは1日、タグ付きは最新3イメージを保持する。
進行中の試験が参照するdigestを保持数から押し出さないよう、試験中に大量のイメージを登録しない。
ソースrevisionとdigestの対応・ARM64イメージの実行可否は成果物配布工程で確認する。TerraformはECR内のdigest存在を参照するが、ソースとの一致を証明しない。

## 今回行う静的検証

以下はAWSバックエンドへ接続せず、設備も作らない。Terraform `>= 1.11`、AWS provider `~> 6.0`を使用する。

```sh
terraform -chdir=infra/aws-test/bootstrap init -backend=false -input=false
terraform -chdir=infra/aws-test/smoke init -backend=false -input=false
terraform -chdir=infra/aws-test/bootstrap validate
terraform -chdir=infra/aws-test/smoke validate
terraform fmt -check -recursive infra/aws-test
```

`tests/*.tftest.hcl`にはモックAWS providerを使うplanテストを用意する。今回はユーザー指示により**実行しない**。
検証対象はアカウント入力とSSO信頼先の整合、state保護、SGの既存／作成時IAM条件、全設備の必須タグ、IAMパス・権限境界、ログ名と許可ARN、ENI待機の依存関係、通信・SQS・保持設定。
provider/backendのアカウント制限や実際のIAM評価、プロキシ疎通、EC2起動成功はモックの合格だけでは保証できない。

## ローカルのアカウント設定

実アカウントID、SSOプロファイル、引受元のSSOロールはGit管理外の`.local/account.json`だけで編集する。
公開するサンプル・構成テストには架空のIDとプロファイルを使い、認証情報そのものはAWS CLIのSSO管理に任せる。

```sh
mkdir -p infra/aws-test/.local
# 初回のみ実行し、既存のローカル設定を上書きしない。
cp -n infra/aws-test/account.example.json infra/aws-test/.local/account.json
```

サンプルの3項目を、確認済みの値へ変更する。信頼先ARNの末尾`*`はSSO割当のsuffixだけを表し、別アカウントや任意の権限セットを許可するワイルドカードにはできない。
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
providerは`allowed_account_ids = [var.expected_account_id]`を維持する。backendは未設定時に実在しないIDだけを許可し、生成ファイルを渡し忘れた接続を拒否する。
実行中の環境がある間は接続設定を変更しない。将来の別アカウント移行は、全試験の削除とbootstrap stateの扱いを別途決めてから行う。

## 後で構築plan・削除planを確認する手順

以下は常設基盤の作成とイメージ配布、自動削除・結果回収を実装した後の操作手順。**この工程ではapplyを実行しない。**
リポジトリルートから実行する。

```sh
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

```sh
export TF_DATA_DIR="$PWD/infra/aws-test/smoke/.terraform/20260911-01"
terraform -chdir=infra/aws-test/smoke init -reconfigure -input=false \
  -backend-config=../.local/smoke.tfbackend \
  -backend-config='key=smoke/20260911-01/terraform.tfstate'
terraform -chdir=infra/aws-test/smoke plan \
  -var-file=../.local/smoke.tfvars.json \
  -var-file=../.local/20260911-01.tfvars -out=smoke.tfplan
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
  -var-file=../.local/20260911-01.tfvars -out=destroy.tfplan
terraform -chdir=infra/aws-test/smoke show destroy.tfplan
```

上記は削除候補を確認するだけで、削除は実行しない。stateキー・入力・出力の実行IDが一致することを確認する。
実際のdestroyはENI待機スクリプトを使うため、実行元にPython3とAWS CLI v2、および有効なSSO認証が必要。

## イメージのビルド・配布担当

初回の担当は、ローカル設定の**テスト管理者**。構築ロールにはECR push権限を追加しない。
本番向けGitHub Actionsは本番用ロール・ECRに接続するため、この試験用配布には使用しない。将来CI化する場合はテストアカウント専用の配布権限を用意する。
以下は配布担当が後で実行する手順であり、この変更ではbuild・pushを行わない。

1. 対象commitのクリーンなcheckoutを用意する。ローカルテストの合否と対応revisionを記録する。
2. そのcheckoutから、既存DockerfileでARM64イメージを作る。テスト向けにコードを書き換えず、本番へ出す候補成果物として扱う。
3. テスト管理者でログインし、bootstrapのECRへ一意なタグでpushする。イメージを後から本番へ配布するときも、同じ成果物をコピーする。
4. ECRからdigestを取得し、対応するcommit SHAと一緒に試験入力へ保存する。

上記のテスト管理者ログイン・アカウント照合後、クリーンなcheckoutのルートで実行する。

```sh
set -euo pipefail
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

以下は**次工程で実装するコマンド**であり、現時点ではMakefileへ追加していない。

| コマンド | 次工程で実装する責務 |
|---|---|
| `make aws-smoke` | 構築→DB準備→上限300秒の試験→結果回収→削除→残存確認 |
| `make aws-smoke-destroy RUN_ID=...` | 対象実行だけの削除・再試行・残存確認 |
| `make aws-smoke-status RUN_ID=...` | 保存済み結果と最新の残存状況の確認 |

試験の300秒は**構築・DB準備完了後**に起算する。構築・準備・回収・削除には別の有限期限を設ける。
試験成功と削除成功を別々に記録し、未実行・確認不能を成功にしない。実行元の電源断等に備える独立したAWS側の削除監視は、今回の範囲には含めない。

## 費用の目安

事前に取得した東京リージョンのオンデマンド単価に基づく。請求額の保証ではなく、利用開始時に再確認する。

| 計算リソース | 時間単価（USD） |
|---|---:|
| RDS PostgreSQL `db.t4g.micro` | 0.0250 |
| proxy EC2 `t4g.nano` | 0.0054 |
| runner EC2 `t4g.small` | 0.0216 |
| **計算料金の合計** | **約0.052/時** |

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
