# SSM踏み台・SQS VPCE経由の再投入検証（2026-10-04 JST）

## 結論

PC上でAssumeRoleした運用ロールの一時資格情報を使い、SSMトンネル → EC2 → SQS Interface endpoint経由で再投入が成功した。
EC2の付与ポリシーは実AWSで`AmazonSSMManagedInstanceCore`だけ、inline policyなしであることを確認した。
IAMのSendMessageを対象キューに限定して条件を外し、キュー側の指定VPCE制限とCalledViaLast例外を維持する構成が成立した。

同じロールによる公開経路からの直接送信と、VPCE経由の対象外キューへの送信は拒否された。
一方、指定VPCE経由の対象キューへの直接送信は許可される。「再投入だけ許可」を保証する構成ではない。

## 実行条件

- 専用テストアカウント733360597472、東京、run_id `20261004-024124-vpce`。
- source/DLQ 4組と対象外キュー、限定運用ロール4個、private VPC、踏み台EC2 1台、SSM/ssmmessages/SQS endpoint 3個など、合計43 Terraformリソース。
- public IP、NAT、IGW、SSH入口、DB、Consumerは作成していない。
- SSM Agent `3.3.5226.0`、PCのSession Manager plugin `1.2.835.0`。
- SSMトンネルはcontrol運用ロール、SQS比較は各キュー専用の運用ロールで実行。構築・ダミー投入・結果回収・削除は管理者。
- 認証情報はPCのメモリ・子プロセス環境のみ。TLSの証明書検証を維持し、hosts編集や証明書検証無効化は行っていない。

## SQSの実測

各sourceにダミー本文を1件投入し、受信回数超過でDLQへ移した後に試験した。
成功判定はStart APIの成功に加え、taskのCOMPLETEDと元のダミー本文の到着を照合した。

| ケース | VPCE経由の再投入 | VPCE経由の直接送信 | 公開経路の直接送信 | 対象外への送信 |
|---|---|---|---|---|
| 現行IAM条件＋CalledViaLastキュー例外 | 拒否：identity policyがSendMessageを許可しない | 拒否 | 拒否 | 拒否 |
| IAM条件なし＋CalledViaLastキュー例外 | 成功・本文到着 | 許可 | 拒否 | 拒否 |
| IAM条件なし＋ViaAWSServiceキュー例外 | 成功・本文到着 | 許可 | 拒否 | 拒否 |
| 対照：IAM条件なし＋TLSだけのキュー | 成功・本文到着 | 許可 | 今回未再試験 | 拒否 |

現行IAMのCalledViaLast条件は、VPC内から開始する場合も障害になる。
CalledViaLastのキュー例外で成功したため、ViaAWSServiceへ変更する必要性は今回の結果からはない。
公開経路の前回結果は[RESULTS.md](RESULTS.md)を参照。

## SSMの実測

| 操作 | 結果 |
|---|---|
| document未指定のshell | AccessDeniedException |
| 明示的なshell document | AccessDeniedException |
| 汎用remote-host forwarding document | AccessDeniedException |
| SQS東京の443に固定した専用document | 成功 |
| 別の運用ロールからのセッション終了 | AccessDeniedException |
| 開始した本人によるセッション終了 | 成功 |

StartSessionはinstanceの`BoolIfExists ssm:SessionDocumentAccessCheck=true`と、専用documentのAllowを組み合わせた。
最初の`Bool`指定では固定documentまで拒否されたため修正し、許可・拒否の両方を再試験した。
終了・再開権限は本人を表すsystem tagと対象instanceのsystem tagで制限した。
`ssmmessages:OpenDataChannel`は公式例に沿い`session/${aws:userid}-*`へ許可した状態で実トンネルが成功した。
この権限を外した比較は行っておらず、実際の条件キー一覧やAWS内部の評価順序は取得していない。

## 構築時の失敗と回復

初回applyでSQS endpointの作成だけが`InvalidPolicyDocument: UnknownError`になり、依存する4つのsource queue policyも未作成になった。
作成済み38リソースを維持し、残り5件だけのplanを照合したうえで、ポリシー変更なしで再適用すると成功した。
IAM伝播遅延が原因候補だが、AWS内部の原因は未確定。

CONNECT proxyの最初の単体試験でbytearrayの集合比較が失敗し、bytesへ変換して修正した。
Terraform mockテストは実行用tfvarsの影響を受けないよう、公開経路側の入力を明示して修正した。

## 本番への反映案

1. 既存のDB踏み台からSQS endpointへの443送信と、endpoint側の受信を許可する。
2. 固定SQS宛先のSSM Session documentを作り、運用ロールには対象踏み台とそのdocumentの接続・必要なchannel・本人sessionの終了権限だけを追加する。
3. SQS endpoint policyに`vector-operations`と対象キュー/DLQの必要アクションを追加する。
4. 運用ロールの対象キューへのSendMessageからCalledViaLast条件を外す。
5. キュー側の指定VPCE制限・CalledViaLast例外・TLS制限を維持する。

EC2のインスタンスロールにSQS権限を追加する必要はない。
現行の踏み台はTerraform上で一時作成される設計なので、再投入時の作成・撤去と接続対象の特定も本番手順に含める。

## 検証の限界

本番Terraform・本番AWS・本番メッセージは変更していない。
本番ReadOnly → vector-operationsの入口、本番固有のSCP/境界、DB接続との同時利用、KMS、Consumerの処理結果は未検証。
別VPCEからの拒否や実行中taskのCancelも未試験。今回の否定試験は公開経路、対象外キュー、SSMの操作境界を対象とした。
VPCE policyには移送の依存アクションも許可しており、StartMessageMoveTaskだけに削れるかの比較は行っていない。

## ローカル検証

Terraform fmt/validate、mock 4ケース、Python unittest 13ケース、ruff lint/formatが成功した。
追加のSSM 6ケースとSQS 4ケースは上記のとおり実AWSで実行した。
GitHub CIは未実行で、検証コード・結果は未コミット。本番のコードやAWSへの反映はしていない。

## 証拠

Git管理外の`.local/vpce/`に保存した。

- `run.json`：実行ID・fixture・ソースのハッシュ。
- `seed-plan-summary.json`、`reconcile-plan-summary.json`、`verify-plan-summary.json`：新規43件、残り5件、SSM4件とqueue3件更新の照合。
- `ssm-access.json`：SSM許可・拒否6ケース。
- `result.json`：4ケースの呼び出し元・実IAM/queue policy・SQS API結果・本文到着確認。
- `pre-destroy-snapshot.json`：実行中taskなし、SSM sessionなし、実endpoint policy、EC2のSSM専用権限。
- `destroy-plan-summary.json`、`destroy-apply.log`：43件の削除計画と実行。

## 後片付け

43 Terraformリソースの削除が完了した。2026-10-04T02:58:04.807873+00:00 に、Terraform stateが空で、今回のキュー・IAMロール・instance profile・VPC・VPCE・SG・未終了EC2・専用SSM document・ルートEBSがすべて0件であることを実AWSで確認した。
証拠は`.local/vpce/cleanup-result.json`。失敗ケースのDLQに残ったダミー1件と直接送信試験の残存ダミーもキューとともに削除された。

## 一次資料

- [SQS redriveの権限・VPCE制限](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)
- [SSMの接続制限例](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-examples.html)
- [AWS CLIのproxy設定](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-proxy.html)
