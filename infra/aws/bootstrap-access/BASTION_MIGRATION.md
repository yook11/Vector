# 固定Automationへの初回移行

## 作業定義と境界

- Problem: 日常の踏み台作成・撤去を管理者のTerraform applyから運用ロールの固定手順へ移す。
- Evidence: platform_bastion.tf、bootstrap-access、共通bastion-automation module、過去のEC2直接作成検証、AWS公式の数値版ドキュメント認可。
- Invariants: 環境ごとに固定primary ENIを1個とし、同時稼働は最大1台。既存DB/SQS境界とEC2のSSM専用権限を維持する。撤去は明示ID、失敗時cleanupは作成実行IDで限定する。
- Non-goals: 時間による自動撤去、運用ロールへの直接RunInstances付与、DB・アプリ変更、本番適用、実メッセージの再投入。
- Done: コード・手順・単体／Terraform mock・隔離AWS実測・検証資源の撤去が揃い、レビューできる。

この文書は本番適用を実行済みとする証拠ではない。最新main・対象PR・実AWS・両Terraform stateを照合して実施する。

## 1. 利用停止とバックアップ

踏み台のDB/SQS利用とSSMセッションを終了し、通常インフラapplyとbootstrap-access applyを重ねない。
`verify-aws-profile.sh vector-admin`で本番管理者callerを確認する。
管理者専用の安全な保存先に通常インフラとbootstrap-access両方のstateをバックアップする。stateをコミット・共有しない。

```bash
terraform -chdir=infra/aws state list
terraform -chdir=infra/aws/bootstrap-access state list
```

旧アドレスがあるか確認する:

- `aws_instance.bastion[0]`
- `aws_iam_role.bastion[0]`
- `aws_iam_instance_profile.bastion[0]`
- `aws_iam_role_policy_attachment.bastion_ssm[0]`

存在しないIAMを移管・importしない。存在する場合は名前・path・trust・policy attachmentを記録する。
旧`enable_db_bastion`は廃止するため、管理者のtfvarsから除去する。

## 2. 通常インフラの移行

通常インフラのplanを保存して確認する。
既存subnet・SG・規則・ssmmessages endpointは`moved`で同じAWS IDを保つ。
旧EC2は削除、固定ENIは1個作成する。EC2が使っていたroot EBSの削除設定も確認する。
IAM role/profile/attachmentの`removed { destroy = false }`はAWSリソースを残してstateから外す。

期待と異なる再作成・IAM削除・アプリ側ポリシー変更があればapplyしない。
適用後に旧EC2とroot EBSが消えたこと、IAMがAWS上に残り通常stateから外れたことを確認する。

```bash
terraform -chdir=infra/aws output -json bastion_network
```

出力したVPC/subnet/SG/ENIを管理者用tfvarsの`bastion_network`へ渡す。
この間は旧方式のbranchからapplyしない。通常CIからIAM/runbookの管理権限を追加しない。

## 3. 既存IAMのimport

通常stateから外れたことを確認してから、存在するリソースだけをbootstrap-accessへimportする。
以下の`vector-bastion`とpathは実AWS・移行前stateと一致することを確認する。

```bash
terraform -chdir=infra/aws/bootstrap-access import module.bastion.aws_iam_role.bastion vector-bastion
terraform -chdir=infra/aws/bootstrap-access import module.bastion.aws_iam_instance_profile.bastion vector-bastion
terraform -chdir=infra/aws/bootstrap-access import module.bastion.aws_iam_role_policy_attachment.ssm vector-bastion/arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```

roleのpathは`/vector-ops/`、instance profileは既存の`/`を維持する。
二重state管理を残さない。未作成のIAMは次の管理者planで新規作成する。
途中失敗時はバックアップを無条件でpushせず、実AWSと両stateを照合して不足分だけを再開する。

## 4. 管理者用基盤の適用

bootstrap-accessのplanで次を確認して適用する:

- EC2 SSM role/profileは維持または未作成分だけ作成。
- AL2023 arm64・t4g.nano・暗号化8GiB gp3・IMDSv2・固定ENI・UserDataなしのLT。
- SSM専用のAutomation実行ロール、数値版create/destroy runbook。
- 運用ロールへの固定document ARN・承認版・限定PassRoleと状態参照。
- 既存運用ロール最大1時間・SQS5組・固定SQSセッション・DB境界は維持。

AMI更新は管理者がTerraformで行う。LTとrunbookの版が変わるため、踏み台がない時間に更新する。
旧版で作った機体はcreateで再利用せず、明示destroyは同じ管理対象LTであることを照合して撤去できる。

## 5. 運用経路の確認

[運用手順](OPERATIONS.md)でReadOnlyから作成・固定SQS接続・status・撤去を確認する。
実メッセージの再投入は原因修正と対象確認後の別作業とし、Consumer結果まで記録する。
最後に通常Terraformのplanを確認し、Automationが作成したEC2を追加・削除する差分が出ないことを確認する。

## 失敗・競合の扱い

- 固定ENIは同時に1台にしか接続できないため、作成競合で別ENIへフォールバックしない。
- 起動応答喪失時はClientToken=Automation実行IDで照合する。未確定なら成功としない。
- 作成失敗cleanupはその実行が作ったEC2のみ。再利用した機体は削除しない。
- 既存SSMセッションがあればdestroyは拒否する。確認直後の新規SSM開始との完全な排他は提供しないため、作業終了を操作者間で揃える。
- 終了開始後に別のcreateが後継機を作っても、destroyは元のInstanceIdだけを追跡する。
- CLIが終了してもAutomationは続く。開始時の実行ID、失敗時のEC2 IDを残して照合する。
- Automation履歴のGetAutomationExecutionとEC2 Describeは同一アカウント・東京の参照権限を持つ。CLI表示は対象runbookと必要なID・状態に制限する。
