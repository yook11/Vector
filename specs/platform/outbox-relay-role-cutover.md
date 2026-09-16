# Outbox RelayのDB接続ロール切替

## 作業定義

- Problem: DBに作成・権限付与済みのvector_outbox_relayを、4つのRelayの実際の接続に使用する。
- Evidence: z23、各RelayのDATABASE_URL・実行ポリシー・権限境界、Terraform mock test、既存image digest保持処理を照合する。
- Invariants: Relayの接続先ユーザーだけを切り替える。更新途中の旧設定でも接続を維持する。Consumer・Scheduler・キュー・ネットワーク・image digest・起動状態は維持する。
- Non-goals: DB migration、アプリコード変更、ログ改善、DBのvector_app自体の権限削除。
- Done: 新旧接続許可を保持した切替設定とテスト、適用順序、旧許可を削除する条件を揃える。

## 変更内容

Embedding・Assessment・Curation・CompletionのRelayはDATABASE_URLをvector_outbox_relayへ変更する。共通URL生成に同ユーザーを追加し、DB IAM認証とTLSは維持する。

移行中は4つのRelayの実行ポリシーと権限境界でvector_app・vector_outbox_relayの両方への接続を許可する。Lambda設定の更新前や実行中の旧設定からの接続を維持するためであり、恒久的な許可仕様ではない。新規設定は専用ロールだけを使用する。

## 適用順序

1. vector_outbox_relayの作成とz23適用成功を確認する。
2. 最新コードと既存state・tfvarsを使用し、管理者のbootstrap planで4つのRelay権限境界への接続許可追加だけであることを確認して適用する。db_role_master_secret_arnなどの既存入力は維持する。
3. bootstrapの反映を確認してから、本体のAWS terraform applyを承認する。既存image digestを保持し、4つのRelayの実行ポリシーと接続URL以外に想定外の差分がないことを確認する。未配備のRelayを新規起動しない。
4. 配備済みRelayのLastUpdateStatusがSuccessfulとなり、新ユーザーの設定、既存の配送処理、認証・権限エラーがないことを確認する。更新前の実行が終了するまで旧許可を保持する。停止中のRelayは起動状態を維持する。
5. 確認完了後の別PRで、実行ポリシーと権限境界からvector_appへの接続許可を削除する。全Relayが専用ユーザーで動く状態を確認するまで削除を適用しない。

切替に失敗した場合は旧許可を残したまま、接続URLをvector_appへ戻す変更を通常の承認付きTerraform経路で適用する。DBのGRANTや既存ロールは削除しない。

Lambda更新中の呼び出しは旧設定を使うため、depends_onだけでは移行中の認証を保証できない。[AWSの更新時の状態](https://docs.aws.amazon.com/lambda/latest/dg/functions-states.html)を参照する。

## 検証

各RelayのTerraformテストで新ユーザーのURLと新旧2ロールに限定した実行ポリシーを確認し、bootstrap側では各境界が同じ2ロールに限定されることを確認する。既存Consumerの接続先、image、配送先、起動設定は既存テストで確認する。両stackのfmt・validate・mock testと既存image保持スクリプトのテストを実行する。
