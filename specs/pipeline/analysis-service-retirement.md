# ECS analysisサービスと旧Curation経路の撤去

## 作業定義

- Problem: Curation・Assessment・EmbeddingがOutbox・SQS・Lambdaへ移り、ECSのanalysisサービスに固有の仕事が無くなった。サービスと、残っている旧Curation・maintenanceのコードを撤去する。
- Evidence: analysisの起動定義、旧Curation taskとその呼び出し元、maintenanceの3 task、サービスから派生するAWS構成、queue観測に依存する監視を照合した。旧Curation streamの未処理は移行後0で推移している。
- Invariants: 新しいLambda経路(Consumer・Outbox relay・backfill)と、analysis以外の段の構成・権限・監視を変えない。
- Non-goals: パイプライン観測のCloudWatchへの移設、工程別の失敗率監視の見直し、DLQの整理、権限境界のサービス名一覧の更新。
- Done: analysisサービスと旧コードが無く、他サービスと新経路が撤去前と同じく動作し、撤去に起因する通知が出ない。

## 2段階に分ける理由

旧コードの削除を先に反映すると、起動定義を含まないイメージが残存するanalysisサービスへ配られ、タスクが起動失敗を繰り返す。
このため1段目でサービスを撤去し、2段目で旧コードを削除する。2段目は1段目の本番反映を確認してから着手する。

- 1段目(Terraformのみ): analysisの段定義、frontend向けの通信許可、brokerの専用ユーザー、queue観測に依存する滞留・観測死活の監視を撤去する。アプリの反映は伴わない。
- 2段目(コードとTerraform): 旧Curation task・失敗処理・stage hold・日次上限、maintenanceのtaskとbroker・scheduler、起動定義、関連テストを削除する。

## 1段目と2段目の間の状態

- `pipeline_events`の保持期間による削除と、queueの観測が止まる。観測の責任はログ・メトリクスの整備へ移す。
- schedulerは受け手のいないmaintenance streamへ定期投入を続ける。処理されないだけで他の経路へ影響しない。
- この間隔は短く保つ。

## 2段目で外す権限

- schedulerのmaintenance stream書き込みと、fetchのCuration stream書き込み。
- 旧schedulerが投入をやめた後に外す。2段目はアプリを先に反映し、その後にTerraformを適用する。

## 検証

- 1段目: Terraformのfmt・validate・mock test。実環境のplanが、analysisから派生する資源と対象監視の撤去、broker利用者一覧の更新、egress proxyの許可一覧更新に限られることを確認する。
- 2段目: Ruff、backend unit、integration、Terraformのvalidate・mock test。削除したmodule・symbolの残存参照を検索する。
- 反映後: サービス一覧、各LambdaのActiveと処理継続、SQS・DLQ、撤去に起因する通知が無いことを確認する。
