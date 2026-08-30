# infra/aws/bootstrap — 統制の土台

**admin が手元から 1 回だけ apply する。以後ほぼ触らない。** local state。

本体スタック (`../`) が CI から回るために先に存在している必要があるものだけを置く。

| ここに置くもの | 理由 |
|---|---|
| state bucket | 本体の backend。自分の置き場は自分では作れない (鶏卵) |
| GitHub OIDC provider | CI が AWS に入る唯一の入口 |
| **CI ロール 3 つ** | apply が自分自身を書き換えられないようにするため |
| **permissions boundary** | 同上。apply が天井を書き換えられないようにするため |

## なぜ CI ロールと boundary が「本体」ではなくここなのか

これがこのディレクトリの存在理由。

`iam:CreateRole` と `iam:AttachRolePolicy` を持つロールは、AdministratorAccess を
付けた新しいロールを作って引き受けられる。**`terraform-apply` は放っておくと実質
管理者**で、そうなると段ごとに権限を分けた意味が消える。

対策は permissions boundary だが、**boundary は迂回路を全部塞いだときだけ構造になる**。

| 塞ぐもの | どこで |
|---|---|
| 想定外の boundary でのロール作成 | `DenyRoleCreationWithoutBoundary` |
| 用途別ロールへの広い boundary の付け替え | `DenyWideBoundaryOnAgentCoreRoles` |
| boundary の剥奪・差し替え | `DenyBoundaryTampering` |
| **boundary policy 自体の書き換え** | `DenyTouchingCiScope` |
| **CI ロールと OIDC provider の改変** | `DenyTouchingCiScope` |
| `iam:PassRole` の乱用 | `PassRoleToEcsOnly` |

boundary policy 自体の書き換えが最も見落としやすい。boundary は managed policy
なので、`iam:CreatePolicyVersion` を許すと **「boundary は必ず付くが、その中身が
Administrator」** が通る。CI ロール自体の改変が抜けると「自分の Deny を自分で
消す」が通り、Deny 全体が運用の約束に退化する。

boundary は用途ごとに分ける。天井は「そのロールの policy が壊れたときどこまで
届くか」を決めるものなので、1 本に統合すると天井が全用途の和集合まで広がる。
ただし許可リストにした時点で「CI はこの中から選べる」という意味になるため、
**分割だけでは制御にならない**。ロール名と boundary を対で縛って初めて、
分割が天井の縮小として効く。

**path で Allow 側からも成立させている。** 本体が作るロールは `/vector/`、
CI ロールと boundary は `/vector-ci/`。apply の `iam:*` は `/vector/` にしか
届かないので、`/vector-ci/` への Deny は**二重底**になる。
Deny を消されても Allow が無い。

## 限界 (残余リスクを認める)

**root は常に全部できる。** これの組織版は SCP だが管理アカウントからの適用が前提。
アカウント内では boundary + 条件付き Deny が到達可能な最強で、
「自分に課す統制」であって外から強制される統制ではない。
root アカウントは MFA を有効にし、アクセスキーを作らない。

**apply は `/vector/` 配下になら任意の trust policy を持つロールを作れる。**
外部アカウントを信頼する形も書ける。boundary が実効権限の上限を切るので実害は
小さいが、**blast radius の定義は boundary の中身そのもの**であって、
「apply は何も悪いことができない」ではない。

**service-linked role は bootstrap が作る。** ECS / ALB / RDS / ElastiCache は
初回作成時に `iam:CreateServiceLinkedRole` を暗黙に呼ぶ。`/aws-service-role/` path
なので apply の `/vector/` scoping では届かない。apply 側に穴を開けず、
admin が 1 回で作る側に寄せた (`service_linked_roles.tf`)。

## secret を CI に読ませない

`plan` / `apply` / `deploy` の 3 ロールすべてに、`ssm:GetParameter*` /
`secretsmanager:GetSecretValue` / `kms:Decrypt` の Deny を入れてある。

**SSM parameter は Terraform で管理しない。** `aws_ssm_parameter` の `value` は
provider schema 上 `computed` なので、`ignore_changes = [value]` を付けても
**refresh のたびに実値が state に書き戻される**。箱だけ宣言する方式は成立しない。

したがって parameter は CLI で作る (`aws ssm put-parameter`)。Terraform 側は
ECS task definition の `valueFrom` と IAM policy の path で参照するだけで、
値には触れない。**この不変条件を IAM の Deny で担保している**ので、うっかり
`data "aws_ssm_parameter"` を足しても apply が失敗して気づく。

## plan は read-only にならない

`use_lockfile` の native locking は state bucket に lock オブジェクトを書く。
`plan` ロールは書き込めないので、**CI の plan は `terraform plan -lock=false`**
で走らせる。lock を取るのは apply だけ。

## 承認の主張は正確に

apply ロールの trust policy は `repo:<owner>/<repo>:environment:production` の
sub claim を要求する。ただし **IAM が検証するのは「その claim を持つ token か」
だけ**で、その Environment に承認を必須にするかは GitHub 側の protection rule に残る。

正確には「**承認を経ずにこの sub は発行されない。要件を外すには GitHub の設定変更が
必要で、それは監査ログに残る**」。enforcement point を IAM に移したのではなく、
GitHub の設定を信頼チェーンに組み込んだ。

**`deploy_environment` に required reviewer を設定しないと、この主張は成立しない。**

## public repo の性質

fork からの `pull_request` は `id-token: write` を取れないため、**外部 PR が
plan ロールを assume する経路は最初から存在しない**。

裏返しの禁止事項: **`pull_request_target` で AWS credential を扱わない。**
これをやると外部 PR に credential が渡る。

## 使い方

```
cp terraform.tfvars.example terraform.tfvars   # owner / repo を埋める
terraform init
terraform apply
```

apply 後、出力された `state_bucket_name` を `../versions.tf` の backend ブロックに
書いて有効化し、`permissions_boundary_arn` を本体スタックの variable に渡す。

`terraform.tfstate` は `.gitignore` 済み。secret の実体は入らないが account ID は入る。
