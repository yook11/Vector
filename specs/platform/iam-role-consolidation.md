# IAMロールの概念別統合

Status: 第一歩（backfill）を実装済み（2026-09-22）。本番適用済みで、旧boundaryも撤去済み。配信・AI分析・外部取得は未着手。

## Work Definition

- Problem: Lambda関数1つごとに実行ロール・Schedulerロール・boundaryを複製しているため、関数が増えるたびにbootstrapの共有許可表（ロール作成ガード、boundary対応表、PassRoleガード、設定復号の対象、SQS endpoint）が伸びる。`apply_role_creation` managed policyは2026-09-21時点で6,136字（テスト用prefix）で、IAMの上限6,144字に達した。
- Evidence: `infra/aws/bootstrap/oidc.tf` の `managed_role_arns`・`outbox_service_roles`・`boundary_pairing_statements_by_group`、`infra/aws/bootstrap/role_creation.tf`、各Lambdaのboundary、Terraform mock testのpolicy長断言。
- Invariants: ロールは「同じ振る舞いと同じ到達範囲」で束ねる。概念をまたぐ権限は1ロールに持たせない。各boundaryはno-escalationと関数コードからのENI操作拒否を保つ。CIはboundaryペアリングで縛られたロールしか作れない。切替中に稼働中の関数を作り直さない。
- Non-goals: DBロールの概念分割（別途行う）、Lambda関数自体の統合、概念名の語彙変更、権限の緩和（wildcard化）。
- Done: 5概念（外部取得／AI分析／配信／救済／運用）それぞれが実行ロール1本とboundary1組で動き、関数の追加で共有許可表が伸びない。

## 概念

| 概念 | 主体 | 共通する到達範囲 |
|---|---|---|
| 外部取得 | source-dispatch、acquisition-consumer、completion-consumer | プロキシ経由の外部HTTP、取得系キュー、`vector_collect` |
| AI分析 | curation／assessment／embedding consumer | AI資格情報、分析結果の保存、`vector_app` |
| 配信 | Outbox relay 4本 | Outboxの読み取りと各キューへの送信、`vector_outbox_relay` |
| 救済 | backfill各段 | DBの読み取りと期限切れ整理、各キューへの送信。外部へ出ない |
| 運用 | auth-rate-limit-cleanupなど | 個別 |

救済をAI分析や外部取得へ吸収しないのは、SQSの向き（送信）、DBの読み取り範囲、ネットワーク到達（外へ出ない）、秘密情報の要否がconsumerと異なるためである。概念の中では関数どうしの分離を持たないが、同じイメージ・同じDBロール・同じ信頼境界で動く関数の間の分離は、侵害への防御ではなく誤送信の防止でしかなく、失うものは小さい。

## 切替手順

1. bootstrapで新boundaryと新ロール名を許可表に加え、旧boundaryは残す。管理者経路でapplyする。
2. 本体で新ロールを作り、関数の `role` とscheduleの `role_arn`／groupを切り替え、旧ロールと旧groupを削除する。CI applyは `/${name_prefix}/` 配下に `iam:*` を持つため旧ロールの削除は許可表に依存しない。
3. 旧boundaryは旧ロールに付いている間は削除できないため、2の後にbootstrapから撤去し、段共通boundaryを元のアドレスへ移す。

## Implementation

- 救済（backfill）: 段別ロール6本・boundary 6本を `${name_prefix}-backfill-lambda`／`-scheduler` とboundary 2本へ統合。`apply_role_creation` は6,136→5,380字、`apply_pass_role` は5,148→4,448字、`apply_backfill` は4,138→3,074字（テスト用prefix）。切替後に段別の旧boundary 6本と旧schedule／groupの許可を撤去した。
- 配信、AI分析、外部取得: 未着手。

## Verification

本体mock test 38件、bootstrap 35件、fmt・validate。本番適用後の確認はImplementationに追記する。
