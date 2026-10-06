# 固定SSM Automationの隔離AWS検証

アカウント733360597472・東京・vector-test-adminだけで実行する。本番資源は操作しない。
共通`infra/modules/bastion-automation`を本番と同じソースとして使用する。
既存のdlq-redrive、bastion-lifecycleのコードと実測結果は保持する。

[2026-10-06の実測結果](RESULTS.md)に、成功・拒否・故障注入・撤去の証拠を記録する。

## 実行

AWS CLI、Session Manager plugin、Terraform、既存backend仮想環境のbotocoreを使用する。
追加パッケージは導入しない。CIはmockと単体だけを実行し、applyとprobeは手元だけで行う。

```bash
aws sso login --profile vector-test-admin
mkdir -p infra/aws-test/bastion-automation/.local
terraform -chdir=infra/aws-test/bastion-automation init
terraform -chdir=infra/aws-test/bastion-automation plan -var run_id=YYYYMMDD-01 -out=.local/create.tfplan
terraform -chdir=infra/aws-test/bastion-automation show .local/create.tfplan
terraform -chdir=infra/aws-test/bastion-automation apply .local/create.tfplan
terraform -chdir=infra/aws-test/bastion-automation output -json fixture > infra/aws-test/bastion-automation/.local/fixture.json
backend/.venv/bin/python infra/aws-test/bastion-automation/probe.py positive
backend/.venv/bin/python infra/aws-test/bastion-automation/probe.py negative
backend/.venv/bin/python infra/aws-test/bastion-automation/scope_probe.py
backend/.venv/bin/python infra/aws-test/bastion-automation/fault_probe.py
backend/.venv/bin/python infra/aws-test/bastion-automation/probe.py finish
```

`.local`はGit除外。資格情報はメモリだけで使用し、結果にはID・固定ラベル・状態のみ残す。
失敗時は記録済み実行IDと実AWSを照合してから再開する。既存のEC2が残っている状態でpositive全体を盲目的に再実行しない。

## 最終撤去

先に実行中AutomationとSSMセッションを確認し、終了させる。運用runbookでEC2/root EBSを撤去する。
その後、管理者でTerraformのdestroy planを保存・確認して常設テスト基盤も撤去する。
空stateだけで完了とせず、VPC、管理タグ、IAM名、SSM文書名からAWS上の残存も確認する。
`backend/.venv/bin/python infra/aws-test/bastion-automation/audit.py`で残存件数を記録する。
完了・キャンセル済みAutomation履歴とCloudTrail履歴は削除対象にしない。

故障注入では管理者が専用test documentを一時作成し、EC2作成後にcreateステップの出力を失う状態を再現する。cleanup失敗試験はテスト実行ロールに対象タグ限定の一時TerminateInstances Denyを加え、finallyで解除する。中断時は`test-only-deny-termination`と`-fault-output`文書の残存を確認してから復旧する。

東京1aでは実測中にt4g.nanoのInsufficientInstanceCapacityが返ったため、このfixtureは東京1cに固定する。runbookは別AZへ自動的に切り替えない。

Interactive検証はステップを進める権限の拒否まで確認する。AWSは開始者本人以外によるInteractive終了も拒否するため、検証の後片付け時だけ、テスト運用ロールへ当該実行ARNのStopAutomationExecutionを一時許可し、同じ開始者の資格情報を更新してCancelした後に解除する。本番権限には追加しない。中断時は`test-only-stop-interactive`も残存確認する。

追加の故障注入では、実RunInstances呼び出しが成功した直後に応答だけを捨て、同じ実行IDでの照合を確認する。同時に旧EC2の終了確認を120秒遅延させ、その間に固定ENIへ接続した後継機が削除されないことを測定する。差し替えるのは隔離コピーの文書だけで、最後に`-fault-slow-destroy`と`-fault-launch-response`も削除する。
