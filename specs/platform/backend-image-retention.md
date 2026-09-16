# backendイメージの保持

## 作業定義

- Problem: ECRの件数ベースの削除が、稼働中Lambdaの参照するbackendイメージを削除する。
- Evidence: Embedding ConsumerとOutbox relayの`ImageDeleted`、ECRのイメージ不在、CloudTrailの`PolicyExecutionEvent`、`infra/aws/registry.tf`を照合した。
- Invariants: Lambdaが参照するbackendイメージを保持し、commitタグの不変性と他リポジトリの保持設定を維持する。
- Non-goals: イメージの自動整理機構の追加、DB・認証・他工程の稼働状態変更、DLQの受信・削除・再投入。
- Done: backendの自動削除ポリシーを除去する差分が検証され、本番適用後にポリシー不在とConsumer／relayの正常処理を確認できる。

## 障害の証拠

2026-09-16の調査で、`vector-embedding-consumer`と`vector-outbox-relay`が同じdigestを参照し、`Inactive`／`ImageDeleted`となっていた。

- 削除されたdigest: `sha256:ac60f09a45650336d0ed43ea2edc7c7ca1f929241670366d37f3148ad80d51a4`
- 元のcommitタグ: `69c4953f13224b689c013aa24b20d575c2b1a7f7`
- CloudTrail記録: 2026-09-15 16:08:21 UTC（9月16日01:08:21 JST）の`PolicyExecutionEvent`に、当該digestとrulePriority 1を確認。
- 適用ルール: `tagStatus=any`、`imageCountMoreThan=10`、`expire`。

イメージの削除時刻と、Lambdaが呼び出しに応答できなくなる時刻は同一とは限らない。実際の停止時刻はこの記録だけでは確定しない。

## 保持方針

backendはECSと更新頻度の異なる複数のLambdaが共有しているため、件数による自動削除から除外する。backendリポジトリとイメージ自体は削除せず、`aws_ecr_lifecycle_policy.this["backend"]`だけを除去する。他リポジトリの件数による保持と、全リポジトリのタグ不変性は維持する。

backendの保管量は今後増加する。不要イメージの整理は、稼働中・復旧用の参照を照合して別途行う。保持数を増やすだけでは、長期間同じdigestを使うLambdaを保護できない。

AWSの[ライフサイクルポリシー仕様](https://docs.aws.amazon.com/AmazonECR/latest/userguide/LifecyclePolicies.html)と[Lambdaコンテナのライフサイクル](https://docs.aws.amazon.com/lambda/latest/dg/images-create.html)を参照。

## 復旧と反映

削除ポリシーの除去だけでは、削除済みのイメージを参照するLambdaは復旧しない。ECRに存在し、ARM64と既存handlerの互換性を確認したイメージを明示指定して更新する。

既存の`AWS terraform apply`では、`embedding_consumer_image_digest`と`outbox_relay_image_digest`が対応する入力である。他のdigestと稼働状態は省略して現状を保持する。

このworkflowはmain全体のplanを適用するため、未適用の旧Embedding監視・ACL撤去も含まれる。イメージ更新だけの実行とは扱わず、対象差分を確認してproduction承認を行う。最新main・承認・Terraform stateの整合性確認を迂回しない。

適用後は次を確認する。

1. backendのECRライフサイクルポリシーがなく、他リポジトリのポリシーが維持されている。
2. Consumer／relayが指定digestで`Active`／`Successful`になっている。
3. Consumerの保存成功、relay配送、SQSの滞留、エラー・throttleを確認する。
4. DLQの残件数は別途記録し、今回の復旧に便乗して再投入・削除しない。

## 検証

Terraform fmt／validateとmock testで、backendのポリシー不在、リポジトリとタグ不変性の維持、他リポジトリへの保持数設定の反映を確認する。本番適用結果はローカルテストの結果と分けて記録する。
