# bootstrap-access — 本番bootstrapの実行権限

管理者がこの独立したTerraform構成で`vector-bootstrap-apply`を作成し、
bootstrap専用ユーザーが`VectorBootstrap`から引き受けて既存`../bootstrap`を手動更新する。
この構成のlocal stateと既存bootstrapのlocal stateは別々に保管する。

## 作業定義

- Problem: Vector用policy追加のたびに管理者が許可一覧を更新する往復を減らす。
- Evidence: `../bootstrap/*.tf`のpolicy path・取り付け先、既存SSOの`VectorBootstrap`、`tests/access.tftest.hcl`、AWSのpath・`iam:PolicyARN`条件の仕様を確認する。
- Invariants: 同一アカウントの専用policy pathに管理を限定し、CIロール5つ・用途別の取り付け先・自己変更拒否・SSO入口・秘密値取得拒否を維持する。
- Non-goals: Identity Centerのユーザー・許可セット・割当の変更、既存stateの移動、テストbootstrapの変更、CI化、新規アカウントの初期構築。
- Done: Terraformの整形・validate・モックテストで新規policyの許可と対象外の拒否を確認し、管理者planの結果または未実行理由を記録する。AWSへの適用と専用ロールでの確認は、下記の導入手順で別途実施する。

## 管理対象と境界

| 対象 | 専用ロールに許す操作 |
|---|---|
| `/vector-ci/`の既存CIロール5つ | 作成・更新・削除、trust・inline policy・タグ管理 |
| 同一アカウントの`/vector-ci/`配下のmanaged policy | 新規作成・削除・内容・バージョン・タグ管理（子pathを含む） |
| managed policyの取り付け・取り外し | 固定のplan/applyロールへ、下記の用途別パターンに限定 |
| GitHub OIDC provider | `token.actions.githubusercontent.com`の管理 |
| state bucket | `vector-tfstate-<ACCOUNT_ID>`の設定読取・タグ・versioning・暗号化・public access block・bucket policy更新 |
| public hosted zone | 指定した既存zoneの読取・タグ・コメント更新 |
| service-linked role | ECS・ALB・RDS・ElastiCache・Chatbotの固定名だけ作成・読取・タグ・説明更新 |
| KMS | 東京の`alias/aws/lambda`が付いたキーの`DescribeKey`だけ |

アカウントとzone IDは管理者が確認して入力し、providerの`allowed_account_ids`で別アカウントへの適用を拒否する。
roleのpreconditionでも同じアカウントの`WorkloadAdministrator`を確認する。
専用path内のpolicy追加・改名はbootstrap担当で完結する。
CIロールの追加・改名、管理pathや取り付け規則の変更は、管理者がこの構成を先に更新する。
ロールARNはパス`/`の`role/vector-bootstrap-apply`で、作成済み許可セットの宛先と一致させる。

| 取り付け先 | 許可するpolicy |
|---|---|
| `vector-ci-terraform-plan` | 同一アカウントの`/vector-ci/vector-ci-plan-*`、既存`/vector-ci/vector-ci-lambda-config-readback`、AWS管理の`ReadOnlyAccess` |
| `vector-ci-terraform-apply` | 同一アカウントの`/vector-ci/vector-ci-apply-*`、既存`/vector-ci/vector-ci-lambda-config-readback` |

取り付け条件は`ArnLike`と`iam:PolicyARN`で指定する。実行ロール用boundaryは従来の
`vector-<用途>-boundary`という名前を使い、CIへの取り付け対象に含めない。
名前はpolicy内容を検査するものではなく、plan用に付ける権限が読み取り目的かどうかは差分で確認する。

自分自身への`iam:*`、SSO・Identity Store・Organizations操作、追加のAssumeRole、PassRole、
秘密値の取得・復号、本体S3 stateオブジェクトの読取・書込・削除は明示的に拒否する。
state bucket・hosted zoneの作成・削除とservice-linked roleの削除は管理者の初期構築・復旧作業に残す。
bootstrapのstateはlocalなので、通常更新にS3オブジェクト権限は不要。

**このロールは信頼された権限管理者向けである。** CIのtrust・inline policy・boundaryを書き換えられるため、
CIの別セッションや実行ロールを介した間接的な権限拡大まで防ぐものではない。
OIDCとbucket policyにも他主体へのアクセスを変更する能力がある。
自身への直接変更拒否を、アカウント全体での権限昇格防止やCI承認の絶対的な強制と解釈しない。
bootstrap差分は手動で確認し、通常CIの承認失敗の迂回に使わない。
専用pathにはbootstrap担当に管理を任せられるpolicyだけを置き、実行ロール自身やSSO用のpolicyを移さない。
変更者を追跡できるCloudTrailの記録とSSO認証履歴を確認する。

## 初回導入

1. 管理者が本番アカウント、Identity Centerの実リージョン、既存hosted zone IDを確認する。
   `terraform.tfvars.example`を基にGit管理外の`terraform.tfvars`を用意する。
   既存ファイルがある場合はコピーで上書きせず、入力を照合する。
2. `VectorBootstrap`のinline policyを次の内容と照合する。対象アカウントIDだけ置き換え、
   追加managed policyは付けない。bootstrap専用ユーザーの直接・グループ経由の割当を確認する。

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AssumeVectorBootstrapApply",
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "arn:aws:iam::123456789012:role/vector-bootstrap-apply"
  }]
}
```

3. リポジトリルートで管理者の本人確認とplanを実行する。

```bash
bash infra/aws/scripts/verify-aws-profile.sh vector-admin
terraform -chdir=infra/aws/bootstrap-access init -input=false -lockfile=readonly
terraform -chdir=infra/aws/bootstrap-access plan -input=false -out=bootstrap-access.tfplan
```

4. 初回はロールとinline policyの作成2件だけであること、信頼先が同一アカウントの
   `AWSReservedSSO_VectorBootstrap_*`だけであることを確認する。レビュー後、管理者が適用する。

```bash
terraform -chdir=infra/aws/bootstrap-access apply bootstrap-access.tfplan
```

変更・待ち時間が入った場合は最新設定でplanを作り直す。
このstateを既存bootstrapへimport・移動しない。管理者がstateを安全にバックアップし、
同じ構成のapplyを同時実行しない。`prevent_destroy`は事故防止でありIAMの境界ではない。

## bootstrap専用ログイン

CLIの設定には次の2プロファイルを追加する。例のアカウントID・SSO URLは確認した値に置き換える。
用途別ユーザーの認証を混ぜないよう、管理者やテスト用とは別の`sso-session`を使う。

```ini
[sso-session vector-bootstrap]
sso_start_url = https://YOUR_PORTAL.awsapps.com/start
sso_region = ap-northeast-1
sso_registration_scopes = sso:account:access

[profile vector-bootstrap]
sso_session = vector-bootstrap
sso_account_id = 123456789012
sso_role_name = VectorBootstrap
region = ap-northeast-1

[profile vector-bootstrap-apply]
source_profile = vector-bootstrap
role_arn = arn:aws:iam::123456789012:role/vector-bootstrap-apply
duration_seconds = 3600
region = ap-northeast-1
```

`sso_region`はIdentity Centerの実リージョンに合わせる。MFAはSSO認証側で設定・確認する。
AssumeRoleのtrustではSSOの可変suffixだけを許容する。`us-east-1`のIdentity Centerは
SSO role ARNのregion要素が無い形式で生成する。ロールチェーンのセッション上限は1時間。

```bash
aws sso login --profile vector-bootstrap
bash infra/aws/scripts/verify-aws-profile.sh vector-bootstrap
bash infra/aws/scripts/verify-aws-profile.sh vector-bootstrap-apply
```

ログイン画面ではbootstrap専用ユーザーを使う。既存の管理者ブラウザーセッションと取り違えない。
preflightはアカウントとロールを検査するが、同じPermission Setを割り当てたユーザー同士は区別しない。
人の分離はIdentity Centerの割当と認証履歴で確認する。

## 既存環境へのpath単位の委譲の導入

管理者が既存のbootstrap-accessのstate・tfvarsを使い、この構成を一度plan・applyする。
`VectorBootstrap`許可セット・ロールtrust・CLIプロファイルの変更は不要。
上記の[初回導入](#初回導入)の本人確認・planコマンドを使い、保存planが
既存`manage-vector-bootstrap` inline policyの更新1件だけであることを確認する。
変更はpolicy管理対象の`/vector-ci/*`化と、plan/applyの用途別取り付け条件だけとし、
別の差分がある場合は原因を確認してから適用する。

同じ管理者プロファイルで保存planを適用し、再planに差分がないことを確認する。
続いて`vector-bootstrap-apply`の本人確認と、既存bootstrapのplanを実施する。
ローカルモックだけでは実環境のSCP・IAM設定・AssumeRole成功を証明できないため、
導入後の確認結果も残す。権限確認のためだけに不要なpolicyを作成・削除しない。

## 既存bootstrapの更新

委譲導入後は、新しいConsumer用boundaryや`vector-ci-apply-*` policyを追加するときも、
管理者によるbootstrap-accessの更新は不要。専用ロールでbootstrapを更新し、
最後に本体のTerraformを既存の承認付きCIで適用する。
CIロールと用途別boundaryの対応表は引き続きbootstrapで管理し、本体より先に適用する。

既存`infra/aws/bootstrap/terraform.tfstate`とtfvarsを保持して、専用ロールで新しいplanを作る。
別端末へ移るときも、新しい空stateから適用せず、正しい既存stateを引き継ぐ。

```bash
bash infra/aws/scripts/verify-aws-profile.sh vector-bootstrap-apply
AWS_PROFILE=vector-bootstrap-apply terraform -chdir=infra/aws/bootstrap init -input=false -lockfile=readonly
AWS_PROFILE=vector-bootstrap-apply terraform -chdir=infra/aws/bootstrap plan -input=false -out=bootstrap.tfplan
```

変更対象と拒否される操作の有無を確認した後、同じプロファイルで保存planを適用する。

```bash
AWS_PROFILE=vector-bootstrap-apply terraform -chdir=infra/aws/bootstrap apply bootstrap.tfplan
```

導入時はbootstrap更新後の再planも確認する。想定外の権限不足は必要な操作・対象を確認して
管理者がこの構成を更新し、AdministratorAccessの追加で解消しない。
`infra/aws-test/bootstrap`のManager信頼先変更は別アカウント・別stateのため、
テスト側の最新plan→管理者apply→Manager接続確認を独立して行う。

## 検証

`../bootstrap`のmanaged policyを追加・変更した場合も、この構成の検証を実行する。
bootstrap側のmockテストに加え、新規policyを含むpathの範囲・用途別取り付け先・実行ロールのpolicy容量を確認する。

```bash
terraform -chdir=infra/aws/bootstrap-access fmt -check -recursive
terraform -chdir=infra/aws/bootstrap-access init -backend=false -input=false -lockfile=readonly
terraform -chdir=infra/aws/bootstrap-access validate
terraform -chdir=infra/aws/bootstrap-access test
(cd backend && uv run pytest tests/scripts/test_verify_aws_profile.py -m unit -q)
shellcheck infra/aws/scripts/verify-aws-profile.sh
```

Terraform testはAWSモックを使用する。実環境のSCP・resource policy・SSO割当や、
専用ユーザーのAssumeRole・既存bootstrapのrefresh/apply成功はモックだけでは証明できない。
導入後に専用プロファイルで検証する。

2026-09-13のpath単位委譲の検証では、Terraformの整形確認・validate・モック9件が成功した。
関連するprofile・workflowテスト57件も成功。backend/frontendの実行コードは変更していないため、アプリ全体のテストは対象外。

同日の管理者SSO認証後、実planが`manage-vector-bootstrap` inline policyの更新1件だけであり、
専用policy pathと取り付け条件以外のstatement（すべてのDenyを含む）が不変であることを照合して適用した。
適用結果は追加0・更新1・削除0。実IAMから読み戻したpolicyは保存planと一致し、bootstrap-accessの再planは差分なし。
専用ロールによる既存bootstrapのrefresh・planも成功し、差分なしだった。
新規policyの作成・取り付けAPIは検証目的では実行していない。

導入後の専用ロール確認では、CLIの本人確認は成功した一方、TerraformのSSO取得が401になった。
CLIで取得できる同じ専用ロールの有効な一時資格情報をプロセス内だけで渡し、accountとroleを再確認してplanを実行した。
資格情報の表示・ファイル保存・SSO設定変更は行っていない。通常のprofile経由で次回実行する前に
`aws sso login --profile vector-bootstrap`で専用ユーザーの認証を更新する。

参照: [AWSのSSO trustの形式](https://docs.aws.amazon.com/singlesignon/latest/userguide/referencingpermissionsets.html)、
[AssumeRoleと1時間の上限](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_manage-assume.html)、
[provider 6.56.0の設定](https://github.com/hashicorp/terraform-provider-aws/blob/v6.56.0/website/docs/index.html.markdown)、
[KMS alias条件](https://docs.aws.amazon.com/kms/latest/developerguide/conditions-kms.html#conditions-kms-resource-aliases)。
path単位のpolicy管理と取り付け条件は[AWSのIAMアクセス制御](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_controlling.html)、
wildcardが子pathも含む仕様は[Resource要素](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_resource.html)を参照する。
