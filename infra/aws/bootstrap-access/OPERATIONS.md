# DLQ再投入用の運用ロール

## 作業定義

- Problem: デプロイユーザーが管理者へ切り替えずに、パイプラインのDLQから復旧できるようにする。
- Evidence: VectorDeployの既存AssumeRole権限、bootstrap-accessの管理者管理境界、SQSの5組のRedrivePolicy、AWS公式の再投入要件を確認する。
- Invariants: 信頼元は本番VectorDeployのみ、セッションは1時間、送信先・DLQは5工程の固定ARN、TLSと直接送信のVPC制限を維持する。
- Non-goals: DB読み取り、踏み台、ユーザー追加、通常デプロイ権限の変更、Scheduler/Lambda失敗イベントの独自再投入。
- Done: Terraform検証と権限境界テストが成功し、Identity Centerへの追加内容と本番導入手順が明確である。実運用の完了には適用後のAssumeRoleと再投入の確認を要する。

## 入口と権限

既存のデプロイユーザー → VectorDeploy → vector-operations と切り替える。
新規のSSOユーザーやPermission Setは作成しない。信頼ポリシーは同一アカウントの
VectorDeployロールARNを条件にするため、このPermission Setの割当先全員が対象になる。
割当を別ユーザーへ増やす場合は、運用ロールも使えることを確認する。

対象はsource-acquisition、article-completion、article-curation、article-assessment、
article-embeddingの通常キューと、それぞれの-dlq。東京リージョンの本番に限定する。

- DLQ: StartMessageMoveTask、CancelMessageMoveTask、ListMessageMoveTasks、ReceiveMessage、DeleteMessage、GetQueueAttributes。
- 通常キュー: SQSの代理呼び出しに限ったSendMessage。
- 対象キュー: GetQueueAttributes、GetQueueUrl。
- IAM変更、PurgeQueue、DB接続、任意キューへの送信は付与しない。

受信・削除権限はSQS標準の再投入に必要であり、DLQへの直接操作にも使える。
このロールは閲覧専用ではなく、復旧操作を信頼された運用者へ委譲するもの。
Scheduler失敗DLQとLambda非同期実行失敗キューはSQS標準の再投入に非対応なので含めない。
状況調査のCloudWatch等は既存ReadOnlyを使う。

## 導入

1. 管理者が既存bootstrap-accessのstateとtfvarsを使ってplanし、運用ロールとinline policyの追加だけであることを確認して適用する。別stateで既存リソースを重複作成しない。
2. Identity Center管理権限でVectorDeployの既存inline policyを取得し、outputのdeploy_operations_assume_statementだけを追加する。既存statementを置換・削除しない。対象アカウントへPermission Setを再プロビジョニングする。
3. 通常のインフラ変更経路でoutbox_relay.tfとsource_dispatch.tfのキューポリシー変更を適用する。
4. AWS CLIに以下のプロファイルを追加し、デプロイユーザーのSSOログイン後に本人確認する。

```ini
[profile vector-ops]
role_arn = arn:aws:iam::<ACCOUNT_ID>:role/vector-operations
source_profile = vector-deploy
role_session_name = vector-dlq-operations
duration_seconds = 3600
region = ap-northeast-1
```

5. 再投入前に対象DLQと元キューのARN・件数・RedrivePolicy、Consumerの修正反映を確認する。本文を受信せずにStartMessageMoveTaskで元キューへ毎秒1件から戻す。ListMessageMoveTasksで完了・移動件数を確認する。
6. DLQが空というだけで処理成功と判断せず、Consumerの保存成功・再失敗をログとキュー状態で確認する。悪化時はCancelMessageMoveTaskで残りの移動を中止する。

## 検証と参考

- bootstrap-access: terraform fmt -check、validate、test。
- 本体: terraform fmt -check、validate、test。
- 実環境ではVectorDeployからのAssumeRole成功と、ReadOnlyからの拒否を確認する。
- IAMモックテストはAWSの条件評価や実際の再投入成功を保証しないため、適用後に実動確認する。

[AWS: SSOロールを信頼する方法](https://docs.aws.amazon.com/singlesignon/latest/userguide/referencingpermissionsets.html)
[AWS: DLQ再投入の権限とVPC制限](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)
