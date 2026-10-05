# DLQ再投入権限の実AWS検証結果（2026-10-04 JST）

## 結論

公開エンドポイントから限定ロールで再投入を開始すると、現行設定の拒否を再現した。
キュー側のVPCE制限と、IAM側のSendMessageに付けたCalledViaLast条件は、それぞれ単独でも今回の再投入を拒否した。
IAM側の条件を外すだけ、またはキュー側の例外をViaAWSServiceへ変えるだけでは解決しなかった。

## 実行範囲

- 専用テストアカウント、東京リージョン、run_id `20261003-235303`。
- source/DLQ 4組、対象外確認用キュー1個、限定運用ロール4個と各ポリシー、計25 Terraformリソース。
- 構築・ダミー投入・回収はWorkloadAdministrator、認可試験はAssumeRole後の限定ロールで実行した。
- 各sourceへ1件送り、受信回数超過でDLQへ移動したことを本文で確認してから試験した。
- 本番設定・本番メッセージ・既存のEmbedding smokeリソースは変更していない。

## 結果

| 条件 | 再投入 | 対象キューへの直接送信 | 対象外キューへの送信 |
|---|---|---|---|
| 現行：IAM CalledViaLast条件＋キュー CalledViaLast例外 | 拒否：resource policyの明示的Deny | 拒否 | 拒否 |
| IAM条件なし＋キュー CalledViaLast例外 | 拒否：resource policyの明示的Deny | 拒否 | 拒否 |
| IAM条件なし＋キュー ViaAWSService例外 | 拒否：resource policyの明示的Deny | 拒否 | 拒否 |
| 対照：IAM条件なし＋キューはTLS強制のみ | 成功：COMPLETED・元本文到着・DLQ残数0 | 許可 | 拒否 |
| 追加：IAM CalledViaLast条件＋キューはTLS強制のみ | 拒否：identity policyのAllowなし | 拒否 | 未再実行 |

最初の3ケースは、StartMessageMoveTaskの応答で移動先の `sqs:SendMessage` が拒否され、taskは作成されなかった。
追加ケースは最初の比較結果を保存後、Terraformでキューをseed段階へ戻してcurrentロールだけを試験した。IAMポリシーは変更していない。
追加ケースの対象外送信は最初のcurrentケースで確認済みだが、この段階では再実行していない。

対照ケースの移動件数はCOMPLETED直後に概算0、後続の再取得で1となった。元本文は最初の確認で到着済みだった。
削除前の対照キュー/DLQはともに可視0・処理中0。他3ケースのDLQはダミー1件ずつ、実行中の再投入taskは0だった。

## 検証コードの修正

- IAMのグローバルエンドポイントを明示した呼び出しで、署名リージョンをus-east-1に修正した。SQS/STSは東京を維持した。
- 概算カウンターの即時一致を成功条件から外し、COMPLETEDと元本文到着を確認する。概算値も記録する。
- 最初の中断はIAMの観測APIであり、再投入前だった。次の中断は対照ケースの概算値判定であり、対照の再投入自体は完了していた。
- 対照ケースを再投入し直さず、未実行の3ケースだけを継続し、元の記録へ追記した。
- 修正後のPython単体テスト7件、ruff lint/formatが成功。Terraform定義は実行前のvalidate/mock 3件成功から変更なし。

## 証拠ファイル

Git管理外の `.local/` に、認証情報を含めず保存した。

- `run.json`：実行ID、開始時刻、ソースのハッシュ。
- `seed-plan-summary.json` / `verify-plan-summary.json`：作成25件・変更3件だけであることの照合結果。
- `seed-probe.log`：4ケースのsource → DLQ確認。
- `result.json`：4ケースの呼び出し元・実ポリシー・API応答・対照ケースの本文確認。
- `iam-isolation-result.json`：IAM条件だけを残した追加試験。
- `pre-destroy-snapshot.json`：削除前の全ケースのtaskと残数。
- `destroy-plan-summary.json` / `destroy-apply.log`：今回の25リソースだけの削除計画と実行ログ。

## 検証の限界と次の候補

今回確認したのは公開エンドポイントからの開始であり、実在するVPCE内からの呼び出しは試していない。
AWS内部の実際の条件キー一覧は取得していないため、エラーだけからCalledViaLastキーの有無を断定しない。
本番のReadOnly → vector-operationsという入口、本番固有のSCP/ポリシー、KMS暗号化、Consumerの処理結果も未検証。

対象キューへの直接送信をVPC外では拒否する方針を保つなら、次の検証候補は、IAMのSendMessage条件を外してResourceを限定し、許可したVPCE経由で再投入を開始する構成。実AWSでの追加検証が必要。
公開エンドポイントからの操作を優先する場合は、運用ロールをキューのDenyから除外する案があるが、同じロールの対象キューへの直接送信も許可する変更になる。この案を本番へ適用していない。

## 後片付け

25 Terraformリソースの削除が完了した。2026-10-04T00:10:00.587959+00:00 にテストアカウントを再確認し、Terraform stateは空、今回のprefixのSQSキュー0件・IAMロール0件を確認した。証拠は `.local/cleanup-result.json`。

## 一次資料

- [SQSの再投入権限とVPC制限例外](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)
- [ListMessageMoveTasksの結果仕様](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ListMessageMoveTasksResultEntry.html)
