# Outbox RelayのDB接続ロール切替

## 作業定義

- Problem: 専用ユーザーへの切替後もRelayに残るvector_appへの接続許可を削除する。
- Evidence: z23の適用成功、4つのRelayの設定更新成功・専用ユーザー接続・切替後の定期実行で認証や権限エラーがないこと、既存IAM設定とTerraformテストを照合する。
- Invariants: Relayの実行ポリシーと権限境界のDB接続許可をvector_outbox_relayだけに限定する。接続URL・Consumer・Scheduler・キュー・ネットワーク・image digest・起動状態は維持する。
- Non-goals: DB migration、アプリコード変更、ログ改善、DBのvector_app自体の削除や権限変更。
- Done: 4つのRelayの実行ポリシー・権限境界から旧許可を削除し、専用ユーザーだけを許すテストと適用手順を揃える。

## 最終状態

Embedding・Assessment・Curation・Completion Relayはvector_outbox_relayで接続し、実行ポリシーと権限境界も同ユーザーへのrds-db:connectだけを許可する。DB IAM認証とTLSを維持する。vector_appはConsumerなど他のアプリが継続して使用する。

切替時はLambda更新前や実行中の旧設定からの接続を維持するため、新旧両方のIAM接続許可を一時的に保持した。旧許可の削除は、全Relayの設定更新と旧実行の終了を確認した後に行う。

## 旧接続許可の削除手順

1. 配備済みRelayのLastUpdateStatusがSuccessfulで、DATABASE_URLがvector_outbox_relayであること、更新前の実行が終了し、切替後の定期実行で認証・権限エラーがないことを確認する。
2. bootstrap専用ユーザーで既存state・tfvarsを使用してplanし、4つのRelay権限境界から旧ユーザーの接続許可を削除する差分だけであることを確認して適用する。db_role_master_secret_arnなどの既存入力を維持する。
3. 本体のAWS terraform applyを承認し、4つのRelay実行ポリシーから旧許可を削除する。既存image digest・Lambda接続URL・起動状態を維持し、想定外の差分がないことを確認する。
4. 新ユーザーによる定期実行と、実行ポリシー・権限境界の両方で旧接続許可がなくなったことを確認する。

旧許可削除後は、接続URLだけをvector_appへ戻しても認証できない。切戻しが必要なら、レビュー済み変更でbootstrapの権限境界と本体の実行ポリシーに旧接続許可を戻してからURLを変更する。DBのGRANTや既存ロールは削除しない。

## 検証

各RelayのTerraformテストで専用ユーザーのURLと単一ユーザーに限定した実行ポリシーを確認し、bootstrap側でも各境界が同じユーザーだけを許可することを確認する。既存Consumerの接続先、image、配送先、起動設定は既存テストで確認する。両stackのfmt・validate・mock testを実行する。
