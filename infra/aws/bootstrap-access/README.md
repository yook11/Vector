# bootstrap-access — 本番bootstrapの実行権限

管理者がこの独立したTerraform構成で`vector-bootstrap-apply`を作成し、
bootstrap専用ユーザーが`VectorBootstrap`から引き受けて既存`../bootstrap`を手動更新する。
この構成のlocal stateと既存bootstrapのlocal stateは別々に保管する。

## 作業定義

- Problem: 継続的なbootstrap更新に広い管理者ログインを使う必要を減らす。
- Evidence: `../bootstrap/*.tf`の管理資源、既存SSOの`VectorBootstrap`、AWSのSSO信頼条件を正本とする。
- Invariants: 通常CI・SSO・テストアカウントとの境界を維持し、実行ロールが自身を直接変更できないようにする。
- Non-goals: Identity Centerのユーザー・許可セット・割当のTerraform移管、既存stateの移動、テストbootstrapの変更、CI化、新規アカウントの初期構築。
- Done: 定義とモックテスト・プロファイル検証・管理者によるplanを確認する。AWS適用と専用ユーザーの接続確認は導入時に別途実施する。

## 管理対象と境界

| 対象 | 専用ロールに許す操作 |
|---|---|
| `/vector-ci/`の既存CIロール5つ | 作成・更新・削除、trust・inline policy・タグ管理 |
| bootstrapのmanaged policy 13個 | 内容・バージョン・タグ管理 |
| managed policyの取り付け | plan/applyそれぞれの既存対応表に限定 |
| GitHub OIDC provider | `token.actions.githubusercontent.com`の管理 |
| state bucket | `vector-tfstate-<ACCOUNT_ID>`の設定読取・タグ・versioning・暗号化・public access block・bucket policy更新 |
| public hosted zone | 指定した既存zoneの読取・タグ・コメント更新 |
| service-linked role | ECS・ALB・RDS・ElastiCache・Chatbotの固定名だけ作成・読取・タグ・説明更新 |
| KMS | 東京の`alias/aws/lambda`が付いたキーの`DescribeKey`だけ |

アカウントとzone IDは管理者が確認して入力し、providerの`allowed_account_ids`で別アカウントへの適用を拒否する。
roleのpreconditionでも同じアカウントの`WorkloadAdministrator`を確認する。
管理対象の名前追加・改名は、この構成の許可リストを管理者が更新してから行う。
ロールARNはパス`/`の`role/vector-bootstrap-apply`で、作成済み許可セットの宛先と一致させる。

自分自身への`iam:*`、SSO・Identity Store・Organizations操作、追加のAssumeRole、PassRole、
秘密値の取得・復号、本体S3 stateオブジェクトの読取・書込・削除は明示的に拒否する。
state bucket・hosted zoneの作成・削除とservice-linked roleの削除は管理者の初期構築・復旧作業に残す。
bootstrapのstateはlocalなので、通常更新にS3オブジェクト権限は不要。

**このロールは信頼された権限管理者向けである。** CIのtrust・inline policy・boundaryを書き換えられるため、
CIの別セッションや実行ロールを介した間接的な権限拡大まで防ぐものではない。
OIDCとbucket policyにも他主体へのアクセスを変更する能力がある。
自身への直接変更拒否を、アカウント全体での権限昇格防止やCI承認の絶対的な強制と解釈しない。
bootstrap差分は手動で確認し、通常CIの承認失敗の迂回に使わない。

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

## 既存bootstrapの更新

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

参照: [AWSのSSO trustの形式](https://docs.aws.amazon.com/singlesignon/latest/userguide/referencingpermissionsets.html)、
[AssumeRoleと1時間の上限](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_manage-assume.html)、
[provider 6.56.0の設定](https://github.com/hashicorp/terraform-provider-aws/blob/v6.56.0/website/docs/index.html.markdown)、
[KMS alias条件](https://docs.aws.amazon.com/kms/latest/developerguide/conditions-kms.html#conditions-kms-resource-aliases)。
