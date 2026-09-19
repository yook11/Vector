# DBロール作成の運用経路

## 作業定義

- Problem: 稼働中RDSへのアプリ専用ロール追加を、初期構築SQLや手動の保守接続に依存せず実行できるようにする。
- Evidence: n3・z14・z22の権限migration、db-provision.sql、既存の承認付きmigration workflow、ECSとbootstrap IAMを照合する。
- Invariants: 通常migrationのDB権限を広げない。秘密値をGitHub runner・artifact・ログへ渡さない。mainの承認対象SHAと実行imageを固定する。既存ロールの不整合は自動修正せず停止する。
- Non-goals: ロール削除、任意SQL実行、既存DBの初期化、テーブル権限変更、Relayの接続先変更、既存インフラ管理者権限全体の再設計。
- Done: ロール管理専用の承認付きworkflow・ECS/IAM/ネットワーク定義・runner・ローカル検証を揃え、初回導入と通常追加の手順を示す。

## 責務

`backend/db_roles.json`が作成対象ロールの正本となる。ロール名だけを記述し、属性や任意SQLは入力しない。新規ロールはLOGINを持ち、パスワードや管理属性を持たず、rds_iamへ所属する。初期対象はvector_outbox_relay。

既存ロールは安全属性・IAM所属・所有権を確認し、一致すれば変更しない。不整合があれば全処理をロールバックする。DBレベルのロックでmigrationとの同時実行を防ぐ。テーブル・列の権限は引き続きAlembicが担当する。

## 実行境界

GitHub Actionsから最新mainの固定SHAを使用し、CI・Security成功を確認する。専用imageは毎回そのソースからbuildし、SHA・run ID・run attemptを含む一意のタグでpushする。既存タグは再利用せず停止し、ECRのIMMUTABLE設定で確認後の競合による上書きも拒否する。Buildxのmetadataにあるmanifest digestをECRの登録結果と照合し、一致したbuild由来digestとmanifestのハッシュを記録する。production-db-rolesの承認後、同じSHAが最新mainであることを再確認し、記録されたdigestで専用Fargate taskを起動する。タグから実行imageを再選択しない。全本番変更と同じconcurrency groupに参加する。

専用execution roleだけが対象RDSのmaster secretを取得し、ECSがpasswordをコンテナへ注入する。GitHubのcontrollerはsecretを読めない。task roleはAWS API権限を持たない。DB接続はverify-fullを使用し、接続情報と例外の詳細をログへ出さない。

controller・execution・taskのIAMロールはbootstrapで管理する保護pathに置く。通常のCIロールからのPassRole・IAM変更・専用task起動を拒否する。専用taskはprivate subnetを使用し、RDSとimage・log・secret取得用endpointへの通信だけを許す。

既存の通常Terraform applyには広いRDS管理権限があるため、DB管理者への全到達経路を封鎖した構成とは扱わない。今回の境界は、新設する管理者資格情報の実行経路を通常デプロイから分離することである。

## 導入と通常運用

1. production-db-roles Environmentをmain限定・required reviewerありで作成する。
2. 本体Terraformで専用network・logを作成し、RDS master secretのARN（値は不要）を確認する。
3. 管理者用bootstrapで対象secret ARNを指定して専用IAMを作成し、Environmentへcontroller role ARNを登録する。
4. ロール定義とrunnerを含むmainのCI成功後、AWS DB rolesを起動する。
5. summaryのSHA・対象ロール・image digestを確認して承認する。
6. task成功後、必要なGRANT migrationを別途適用し、その後アプリ接続を切り替える。

ロール追加はmanifestのPRから同じ経路を繰り返す。失敗・取消時は当該runが起動したtaskだけを停止し、原因を確認して新しいrunで再実行する。ロールを自動削除して切り戻すことはしない。

## 初回導入の設定値

- GitHub Environment: `production-db-roles`。既存production-migrationと同じrequired reviewer、mainのみ許可、admin bypass無効とする。
- Environment secret: `AWS_DB_ROLES_ROLE_ARN`。bootstrapの`db_roles_role_arns.controller`を設定する（DBパスワードではない）。
- bootstrap変数: `db_role_master_secret_arn`。RDSのDescribeDBInstancesにある`MasterUserSecret.SecretArn`を指定し、secretの値を取得しない。
- IAM quota: 通常apply roleのmanaged policy attachmentは12本になるため、適用先の上限が12以上であることを確認する。
- 起動: `gh workflow run aws-db-roles.yml --ref main`。任意SQL・任意image・任意ロール名をworkflow入力で受け取らない。

imageのbuildジョブsummaryには対象SHA・ロール名・manifestハッシュ・image digestを表示する。承認前にこのsummaryを確認する。実行結果はworkflowと専用CloudWatch Logs `/ecs/vector-db-roles`で確認する。GRANTのような権限追加は、新しい権限を使うアプリの反映前に適用する。適用順序は変更内容に応じて承認時に確認する。

ECSのsecret注入は専用execution roleとFargate 1.4.0を使用する。実装根拠は[AWS公式のsecret注入仕様](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)を参照。
