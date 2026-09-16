# 旧Taskiq Embedding経路の撤去

## 作業定義

- Problem: 投入停止済みの旧Embeddingを、cron・worker・専用処理・テスト・運用設定から撤去する。
- Evidence: 旧Taskiqの呼び出し元、起動配線、新Consumerとbackfill、監査・監視の使用箇所を照合した。
- Invariants: 新経路の入力検証、生成・冪等保存と成功監査の同一トランザクション、失敗分類、Outbox配送、backfillを維持する。
- Non-goals: 他工程の旧runtime削除、DB schema変更、DLQ操作、Redis履歴・保存済みEmbedding・監査履歴の削除。
- Done: 旧Embeddingの実行入口がなく、共有処理と新経路のテストが通り、本番で旧worker停止と新Consumerの保存継続を確認できる。

## 削除と維持の境界

旧Taskiq task・Trigger・失敗ハンドラー・Taskiq例外変換を削除し、共有ファイルの旧backfill・cron・broker・worker配線・DB pool・hold・backlog・監査・spanからEmbedding専用部分を外す。
新Consumerが使うReady、Service、Repository、AI adapter、分類済み失敗監査、処理結果メトリクス、新backfillは維持する。
公開API・イベント形式・DB schemaは変更しない。Curation／Assessmentの旧runtimeと共通maintenanceは維持する。

旧キューの観測系列と滞留アラームは撤去するが、新経路のEmbedding失敗率アラームとLambda／SQS／DLQ監視は維持する。
旧ECS停止フラグは削除し、Lambda専用settingsの同名フラグは維持する。

## 本番反映前の条件

- 旧Embeddingへの投入停止が反映済みであり、直近の旧キュー観測が成功し滞留していないことを確認する。
- 旧Embedding streamの未配達・処理中と、maintenanceに残る`backfill_embeddings`を参照アクセスで確認する。残タスクは旧handlerで処理を終える。保持済みの履歴件数を未処理件数と混同しない。
- 直接の残タスク確認ができない場合は本番切替を進めない。ECS Exec有効化、権限拡張、キュー削除を確認の代替にしない。
- 新Consumerの保存、新backfillの定期実行、SQS／DLQ、エラー・throttleを確認する。対象0件のbackfillを対象あり再投入の成功とは扱わない。

## マージ後の反映順序

1. 自動起動する`AWS terraform apply`は承認待ちに保つ。今回は通常の「Terraform→APP」と逆に、APPを先に反映する。
2. 最新mainの`AWS app images`を実行し、`production-rollout`で承認する。
3. APP workflowは全revision登録後、schedulerだけを更新する。既存rollout検証器で旧schedulerの終了と新revisionへの収束を確認する。検証失敗・タイムアウト時はworkerを更新しない。
4. scheduler待機後にrelease条件を再検証し、analysisを含む残りのサービスを更新する。旧Embedding workerが起動せず、analysis・maintenanceと新Consumerが動いていることを確認する。
5. 対象SHAとTerraform planを照合して`AWS terraform apply`を承認する。旧Embeddingの監視・ACL・不要設定だけが撤去され、Lambda／SQS／DLQは維持されることを確認する。
6. 旧監視の撤去、新Consumerの保存継続、SQS／DLQ、backfill定期実行、エラー・throttleを再確認する。

APP workflowのscheduler先行検証はコードの混在時間を抑える制御であり、Redisの残タスクを検証するものではない。残タスク確認は反映前の独立した条件として扱う。
APP反映からTerraform適用までの旧Embedding観測アラームは一時的に発生し得る。新経路の障害や`task not found`は、この許容対象に含めない。
TerraformはLambdaの既存image digestを維持するため、この削除のために新Consumerを再デプロイする必要はない。

## 失敗時の扱い

- scheduler更新失敗時は後続workerを更新せず、旧構成のまま原因を確認する。
- APP更新後に問題があればTerraformの承認を保留する。専用ACLが残っている間に、修正またはrevertを通常の承認付きworkflowで反映する。
- ACL撤去後に旧workerを復旧する場合は、先に必要なACLを復元してからAPPを反映する。
- リリースの承認、最新main・CI・適用済みschemaの確認を迂回しない。

## 検証

- 旧専用テストを撤去し、共有テストは新Consumer・新backfillが必要とする保証を維持する。
- 旧taskをpatchしていたAssessmentテストは、現役のService呼び出し契約を検証する。
- Ruff、backend unit、`make test-integration`、Terraform validate／mock testを実行する。
- APP workflowは外部通信を代替した実行テストで、scheduler先行、検証失敗・不在・release条件変更時の後続停止を確認する。
- 削除したmodule・symbolの残存参照を検索し、旧経路の起動・投入が復活しないことを確認する。
