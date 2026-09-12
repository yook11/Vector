# テスト環境のアクセス権限

テスト環境の管理と試験実行を、IAM Identity Centerの2つのカスタムアクセス許可セットに分ける。
bootstrapは既存の管理者による運用を継続し、その権限の細分化は別途扱う。
この文書は採用した割当方針とインラインポリシーの正本であり、AWSへの設定反映や実試験の完了を示すものではない。

## ユーザーへの割当

| ユーザーの用途 | テストアカウントに割り当てるアクセス許可セット | 担当する操作 |
|---|---|---|
| テスト用ユーザー | `VectorTestManager`、`VectorTestRunner` | 環境管理と試験実行を、選択した権限セットごとに行う |
| 既存の管理者ユーザー | `WorkloadAdministrator` | bootstrapと、通常のテスト権限で扱わない管理作業 |

テストアカウントには`VectorDeploy`を割り当てない。現時点では独立したReadOnly用の割当も追加しない。
テスト用ユーザーへ管理者権限を重ねて割り当てず、グループ経由も含めて割当を確認する。
両テスト権限セットを同じユーザーへ割り当てても、選択したSSOロールのセッションごとに権限を使い分ける。試験実行だけを担当する利用者にはRunnerだけを割り当てる。
実ユーザー名・アカウントID・ローカルのプロファイル名は公開文書に保存しない。

## 権限の責務

| 権限セット | 許可する操作 | この権限セットだけでは行わない操作 |
|---|---|---|
| `VectorTestManager` | 既存のTerraform構築ロールの引受、テストECRへのpush・読取、削除時のENI照会 | bootstrapのS3・ECR・IAM作成や権限境界の変更、SSMによる試験投入 |
| `VectorTestRunner` | タグ条件に一致するrunner EC2へのRun Command、SSM稼働状態・コマンド結果の取得、テストログの読取 | Terraform構築ロールの引受、設備作成・削除、ECRへのpush |
| `WorkloadAdministrator` | 現行のbootstrap運用、必要な信頼先更新など | bootstrap専用の最小権限への再設計は後続作業 |

Managerが引き受ける`/vector-test/bootstrap/vector-test-terraform`の権限と制約は[READMEの「使う権限」](README.md#使う権限)を参照する。
Managerにはbootstrap権限を含めないため、このポリシーだけで`terraform -chdir=infra/aws-test/bootstrap apply`は実行できない。
`sts:GetCallerIdentity`は明示的なAllowを必要としないため、アカウント照合用の追加権限は含めない。

両セットとも、信頼されたテスト運用者向けであり、RunIdごとの隔離は提供しない。
Runnerの`AWS-RunShellScript`はrunner上で任意のシェルを実行でき、そのEC2実行ロールに許されたDB接続・管理シークレット読取なども間接的に利用できる。
SSMの稼働状態・結果取得は東京リージョン内の`Resource: "*"`、ログ読取は名前が一致する複数の試験を対象にする。
特定スクリプトだけの実行権限や、特定の試験だけの閲覧権限としては扱わない。

## インラインポリシー

それぞれ同名のカスタムアクセス許可セットへ設定する。`123456789012`はサンプルであり、**すべてテストアカウントIDへ置き換える**。
リージョンは`ap-northeast-1`固定。追加の管理ポリシーによって権限を広げず、実際の割当とプロビジョニング結果も確認する。

### VectorTestManager

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumeTestTerraformRole",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::123456789012:role/vector-test/bootstrap/vector-test-terraform"
    },
    {
      "Sid": "AuthenticateToTestRegionEcr",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:RequestedRegion": "ap-northeast-1"
        }
      }
    },
    {
      "Sid": "PublishAndInspectTestImages",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:DescribeImages",
        "ecr:ListImages",
        "ecr:DescribeRepositories"
      ],
      "Resource": [
        "arn:aws:ecr:ap-northeast-1:123456789012:repository/vector-test/backend",
        "arn:aws:ecr:ap-northeast-1:123456789012:repository/vector-test/proxy"
      ]
    },
    {
      "Sid": "ObserveEniDeletionDuringDestroy",
      "Effect": "Allow",
      "Action": "ec2:DescribeNetworkInterfaces",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:RequestedRegion": "ap-northeast-1"
        }
      }
    }
  ]
}
```

### VectorTestRunner

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "UseRunShellScriptDocument",
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": "arn:aws:ssm:ap-northeast-1::document/AWS-RunShellScript"
    },
    {
      "Sid": "RunCommandsOnTestRunnerInstances",
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": "arn:aws:ec2:ap-northeast-1:123456789012:instance/*",
      "Condition": {
        "StringEquals": {
          "ssm:resourceTag/Project": "vector-test",
          "ssm:resourceTag/Lifecycle": "smoke"
        },
        "StringLike": {
          "ssm:resourceTag/Name": "vector-test-*-runner"
        }
      }
    },
    {
      "Sid": "ReadCommandResultsAndAgentStatus",
      "Effect": "Allow",
      "Action": [
        "ssm:DescribeInstanceInformation",
        "ssm:GetCommandInvocation"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:RequestedRegion": "ap-northeast-1"
        }
      }
    },
    {
      "Sid": "ReadTestLogGroups",
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogStreams",
        "logs:FilterLogEvents"
      ],
      "Resource": [
        "arn:aws:logs:ap-northeast-1:123456789012:log-group:/vector-test/vector-test-*/runner",
        "arn:aws:logs:ap-northeast-1:123456789012:log-group:/vector-test/vector-test-*/runner:*",
        "arn:aws:logs:ap-northeast-1:123456789012:log-group:/vector-test/vector-test-*/lambda",
        "arn:aws:logs:ap-northeast-1:123456789012:log-group:/vector-test/vector-test-*/lambda:*",
        "arn:aws:logs:ap-northeast-1:123456789012:log-group:/vector-test/vector-test-*/proxy",
        "arn:aws:logs:ap-northeast-1:123456789012:log-group:/vector-test/vector-test-*/proxy:*",
        "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/rds/instance/vector-test-*/postgresql",
        "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/rds/instance/vector-test-*/postgresql:*"
      ]
    }
  ]
}
```

## Runnerを使う後続スクリプトの契約

試験スクリプト・自動削除・結果回収は実装済みで、実AWSでは未検証。[操作手順](README.md#実行コマンドの使い方)と[スモークテスト仕様](../../specs/pipeline/embedding-aws-smoke-test.md)を参照する。
このポリシーはCLI・スクリプトに必要なAPIを対象とし、AWSコンソール全画面の閲覧を保証しない。

1. Managerが取得したTerraform出力の`execution.instance_ids.runner`と`execution.log_groups`を受け取る。Runnerにはstateバケットの読取権限を追加しない。
2. `ssm:DescribeInstanceInformation`で対象runnerのSSM稼働状態を確認し、`AWS-RunShellScript`を対象インスタンスIDへ送る。対象は`Project=vector-test`、`Lifecycle=smoke`、`Name=vector-test-*-runner`のすべてに一致する必要がある。
3. Run CommandのCloudWatch出力を有効にし、出力先を`execution.log_groups.runner`に指定する。既定の`/aws/ssm/...`はこのログ読取ポリシーの対象外。
4. Command IDを保存し、`ssm:GetCommandInvocation`で終了状態を取得する。応答内の標準出力だけで全ログを回収済みと判断しない。
5. `logs:FilterLogEvents`で対象ロググループのログをページ送りして回収する。`logs:GetLogEvents`を使う実装はこのポリシーの対象外。

## 利用開始までに必要な設定と確認

ユーザーの割当整理と、Manager／Runnerを使った実運用の確認は別の完了条件として扱う。

- **構築ロールの信頼先を更新する。** 管理者が既存のbootstrap運用で、`trusted_admin_role_arn_pattern`を同一テストアカウントの`AWSReservedSSO_VectorTestManager_*`に変更する。名称に`admin`が残る既存の設定キーを使い、任意のSSOロールを許可するパターンへ広げない。Identity Centerのリージョンに対応した実ロールのパスを確認する。Manager側の`sts:AssumeRole`許可だけでは引受は成立しない。
- **SSOプロファイルを分ける。** 管理者・Manager・RunnerのプロファイルをAWS CLIに登録する。Manager／Runnerはテスト用ユーザーで認証し、それぞれ同名の権限セットを選ぶ。bootstrap用の`.local/account.json`の`aws_profile`は管理者のまま維持する。
- **smokeの接続元を明示する。** `.local/account.json`の`smoke_aws_profile`にManagerを指定し、[READMEの手順](README.md#ローカルのアカウント設定)で再生成する。backendの`profile`とproviderの`aws_profile`の両方へ反映される。`AWS_PROFILE`の切替だけで代用しない。生成ファイルに独自の設定キーを追加しない。
- **実環境で確認する。** 各プロファイルのアカウントとSSOロール、Managerの構築ロール引受・ECR操作、Runnerの対象限定・ログ取得を確認する。自動削除と結果回収の完成まではsmokeをapplyしない。将来の試験では削除時のENI待機も確認する。

この文書更新では、AWSの信頼ポリシー・アクセス許可セット・ローカルプロファイルを変更しない。
bootstrapの最小権限設計、本番環境のbootstrap、試験スクリプトの実装は別途進める。

## 参照

- [IAM Identity Centerのアクセス許可セット](https://docs.aws.amazon.com/singlesignon/latest/userguide/permissionsetsconcept.html)
- [SSOロールARNと信頼ポリシーの注意点](https://docs.aws.amazon.com/singlesignon/latest/userguide/referencingpermissionsets.html)
- [GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html)
- [Run CommandのCloudWatch出力](https://docs.aws.amazon.com/systems-manager/latest/userguide/sysman-rc-setting-up-cwlogs.html)
