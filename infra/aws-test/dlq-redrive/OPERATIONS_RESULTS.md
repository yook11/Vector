# 一時踏み台・運用CLIの実AWS検証（2026-10-06 JST）

## 結論

調査用ロールから運用ロールへ切り替え、同じ運用ロールでSSM接続・SQSの状態取得・DLQ再投入・キャンセルが成功した。
EC2のIDを変更しても、IAMポリシーを変更せずタグ認可で再接続できた。
本番Terraform・CLI・手順は実装済み。本番AWSへの適用と本番メッセージの再投入は行っていない。

## 試験条件

- アカウント`733360597472`、東京、run_id `20261005-223343-ops`（UTC）。
- 新規45リソース。既存比較用4組のsource/DLQ、限定運用ロール、対象外キュー、VPC・SSM専用EC2・Interface endpointに、調査用ロールを追加した。
- `queue-called-via`の1組を今回の運用試験に使用。他の3組はseed後に移送を実行せず、終了時にダミーごと撤去した。
- 調査ロールは対象の属性参照と運用ロールへのAssumeRoleだけを許可し、運用ロールはこの調査ロールを信頼する。
- 本番用CLIと同じ`AwsCli`・`sqs_tunnel`・`operate`を使用する。PCの資格情報取得後の処理を試験アカウント・試験キューに接続した。
- EC2の付与ポリシーは実AWSで`AmazonSSMManagedInstanceCore`のみ、inline policyなしと照合した。
- EC2へ資格情報を渡さず、TLSの証明書検証と通常のSQSホスト名を維持した。

## 実測結果

| 検証 | 結果 |
|---|---|
| 調査ロールによるキュー属性参照 | 成功 |
| 調査ロールによる再投入 | 拒否 |
| 調査ロール → 運用ロール | 成功 |
| 同じ運用ロールでSSM接続とSQS status | 成功 |
| 元キュー由来のダミー1件をDLQから再投入 | COMPLETED、元本文の到着を照合 |
| 指定VPCEから対象通常キューへの直接送信 | 許可 |
| 公開経路から対象通常キューへの直接送信 | 拒否 |
| 指定VPCEから対象外キューへの送信 | 拒否 |
| RUNNINGの再投入タスクをcancel | CANCELLED |
| タグなし／異なるタグのEC2への接続 | 両方拒否 |
| document未指定shell／明示shell／汎用転送 | すべて拒否 |
| SQS東京443に固定したdocument | 許可 |
| 別ロールによるsession終了 | 拒否 |
| 開始したロールによるsession終了 | 許可 |
| EC2撤去・再作成後の接続とstatus | 成功、IAM変更なし |

キャンセル試験は試験DLQにダミー100件を追加し、毎秒1件で開始した。キャンセルAPIの応答時点では概算10件、最終CANCELLEDでは概算30件が移送済みだった。
キャンセルは移動済みメッセージを戻さず、停止完了までに移送が進むことも確認した。

再作成前後のEC2 IDは`i-0425b90b509e51769` → `i-05313c698133d2706`。
撤去planはEC2のdelete 1件、再作成planはcreate 1件だけ。前後のトンネルIAM policyのSHA256は同一で、旧root EBSの削除も確認した。

## 構築・撤去と証拠

初回applyではSQS VPCE作成が`InvalidPolicyDocument: UnknownError`で失敗した。
作成済み40件を保持し、残りのVPCEとsource queue policy 4件だけのplanを照合した後、ポリシー変更なしで再適用して成功した。AWS内部の原因は未確定。

撤去前に全キューのRUNNING／CANCELLINGがないこと、旧・新EC2へのActive SSM sessionが0件であることを確認した。

45リソースのdestroyが完了した。2026-10-06 07:53:29 JSTに、stateが空、今回のキュー・ロール・instance profile・VPC・VPCE・SG・subnet・未終了EC2・SSM document・旧新root EBS・Active SSM sessionがすべて0件であることを実AWSで確認した。

詳細はGit管理外の`.local/operations/`に保存した。

- `run.json`: fixture、旧・新EC2、root volume、実行コードのSHA256、ローカル検証件数。
- `*-plan-summary.json`とapply log: 新規45件、残り5件、キュー制限3件、EC2撤去1件／再作成1件、全体撤去45件。
- `ssm-access.json`: タグ・ドキュメント・セッション所有者の8ケース。
- `result.json`: 同じ運用ロールでの操作と送信境界、本文到着、キャンセル。
- `reconnect.json`: 新EC2での成功と同一IAM policyのSHA256。
- `pre-destroy.json`: タスクの最終状態とActive session 0件。
- `cleanup.json`: destroy後のstateとAWS上の残存確認。

## ローカル検証と限界

| 検証 | 結果 |
|---|---|
| Terraform fmt / validate | 成功（既存の非推奨属性警告あり） |
| bootstrap-access mock | 13件成功 |
| 本体Terraform mock | 50件成功 |
| DLQ検証環境 mock | 5件成功 |
| 運用CLI unittest | 27件成功 |
| 検証コード unittest | 13件成功 |
| 既存profile回帰 pytest | 41件成功 |
| vector-ops分岐の追加確認 | 正常1・拒否3の4ケース成功 |
| 既存smoke runner unittest | 62件成功 |
| ruff lint / format、bash構文、diff check | 成功 |

ローカルではTerraform providerとloopback試験にsandbox外実行が必要だった。既存smoke runnerはworktree内の仮想環境パスを必要としたため、既存環境への一時symlinkで実行し、試験後にsymlinkを撤去した。

GitHub CIは未実行。本番SSO ReadOnlyの入口、本番固有のSCP・権限境界、本番ネットワークへの適用、DBとの同時利用、Consumerの業務処理成功は今回の別アカウント試験では証明していない。
本番CLIの固定アカウント・stage/ARN対応・誤ったcallerの拒否は単体テストで確認した。今回の実AWS試験ではその内部操作に試験専用のキューを渡している。
