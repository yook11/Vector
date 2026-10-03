# DLQ再投入用の運用ロール

## 作業定義

- Problem: ReadOnlyで原因調査した本人が、運用ロールへ切り替えてパイプラインのDLQから復旧できる入口を整える。
- Evidence: 既存の運用ロール・信頼ポリシー・権限境界テストと、AWS公式のSSOロール引受・再投入要件を確認する。
- Invariants: 信頼元は本番ReadOnlyのみ、セッションは1時間、送信先・DLQは5工程の固定ARN、TLSと直接送信のVPC制限を維持する。
- Non-goals: SQS送信条件の変更、DB読み取り、踏み台、ユーザー追加、Scheduler/Lambda失敗イベントの独自再投入。
- Done: 信頼元・output・手順がReadOnlyに揃い、Terraform検証と権限境界テストが成功する。AWSへの適用、送信拒否の解消と実際の再投入は別途検証する。

## 入口と権限

既存ユーザー → ReadOnlyで原因調査 → vector-operationsで復旧操作、と切り替える。
新規のSSOユーザーやPermission Setは作成しない。信頼ポリシーは同一アカウントの
ReadOnlyロールARNを条件にするため、このPermission Setの割当先全員が対象になる。
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
状況調査のCloudWatch等は既存ReadOnlyを使い、ロール切替後にReadOnlyの権限が引き継がれるとは扱わない。

対象ARNに限定した再投入権限は定義済みだが、2026-09-15には移動先キューポリシーの明示的な送信拒否を確認した。信頼元の変更だけでは解消しないため、隔離したキューで再投入成功とVPC外からの直接送信拒否を検証してから送信条件を確定する。

## 導入

1. 管理者が既存bootstrap-accessのstateとtfvarsを使ってplanし、運用ロールの信頼元変更とoutputの改名だけであることを確認して適用する。既存ロールとSQS権限は維持し、別stateでリソースを重複作成しない。
2. Identity Center管理権限でReadOnlyの既存inline policyを取得し、outputのreadonly_operations_assume_statementを追加する。既に同じ許可がある場合は重複追加せず、NoSecretValuesなどの既存statementを維持する。対象アカウントへPermission Setを再プロビジョニングする。
3. ReadOnlyからの引受成功を確認した後、VectorDeployの既存inline policyからvector-operations専用の引受許可を削除し、再プロビジョニングする。他のデプロイ権限は維持する。
4. AWS CLIに以下のプロファイルを追加または更新し、ReadOnlyのSSOログイン後に本人確認する。source_profileはReadOnlyを使うプロファイル名に合わせ、以下は既定プロファイルがReadOnlyの場合とする。

```ini
[profile vector-ops]
role_arn = arn:aws:iam::<ACCOUNT_ID>:role/vector-operations
source_profile = default
role_session_name = vector-dlq-operations
duration_seconds = 3600
region = ap-northeast-1
```

5. 送信条件の検証と必要なキューポリシーの適用が完了してから、対象DLQと元キューのARN・件数・RedrivePolicy、Consumerの修正反映を確認する。本文を受信せずにStartMessageMoveTaskで元キューへ毎秒1件から戻す。ListMessageMoveTasksで完了・移動件数を確認する。
6. DLQが空というだけで処理成功と判断せず、Consumerの保存成功・再失敗をログとキュー状態で確認する。悪化時はCancelMessageMoveTaskで残りの移動を中止する。

## 検証と参考

- bootstrap-access: terraform fmt -check、validate、test。
- 本体のキューポリシーを変更する際は本体でもterraform fmt -check、validate、testを行う。
- 実環境ではReadOnlyからのAssumeRole成功と、VectorDeployからの拒否を確認する。
- IAMモックテストはAWSの条件評価や実際の再投入成功を保証しないため、適用後に実動確認する。

[AWS: SSOロールを信頼する方法](https://docs.aws.amazon.com/singlesignon/latest/userguide/referencingpermissionsets.html)
[AWS: DLQ再投入の権限とVPC制限](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)
