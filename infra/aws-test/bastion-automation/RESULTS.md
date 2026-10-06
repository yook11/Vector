# 固定SSM Automationの実測結果

## 検証対象

- 実施日: 2026-10-06（日本時間）。別アカウント `733360597472`、東京。
- fixture: `vector-test-auto-20261006-01`。本番アカウントへの適用・実メッセージ再投入は未実施。
- ReadOnly相当の隔離ロール → 運用ロールへAssumeRoleして操作した。本番SSOの入口そのものは別途確認する。
- 本番と同じ `infra/modules/bastion-automation` を使用。実AWSのcreate/destroy内のScriptとローカルソースの一致を確認。
- create/destroy文書: 数値版 `4`。Launch Template: 数値版 `2`。
- lifecycle.py SHA-256: `5bb76d79ef9bd602be60350e8865a85195f0d7afdd0e88d1f0fff6ba3b532c19`。
- 実AWS検証時のmainは `dec30911f`。検証した実装差分の基準HEADは `4b4fc4da3`。PR反映前にmain `ac7dd5bc0` も取り込み、今回の変更ファイルと重なりがないことを確認した。

ローカルの `.local/results.jsonl` にID・固定ラベル・状態を記録した。資格情報・メッセージ本文・state・planはこの記録へ含めず、ローカルstate/planと生のAWS応答をコミットしない。

## 成功と拒否の確認

| 条件 | 実測 |
|---|---|
| 同時に2回create | 両方Success、同じ `i-0dc6479187da7babb`、稼働EC2は1台 |
| 既存機のcreate | 同じIDを再利用。CLI本体でも確認 |
| 同じ運用ロールで固定SQS接続 | 成功。EC2ロールはSSM専用 |
| ダミーDLQ再投入 | タスク完了に加え、通常キューで固有マーカーを受信して確認 |
| 指定VPCEからの対象通常キューへの直接送信 | 成功 |
| 公開経路の直接送信・対象外キュー送信 | AccessDenied |
| SSM接続中のdestroy | `ActiveSessionRefused`、EC2維持 |
| destroy → create | 元EC2/root EBSを削除、固定ENIを再利用して別IDで起動 |
| 対象外の実在EC2をdestroy | `UnexpectedInstance`、対象外EC2は維持 |
| タグなし・異なるタグでSSM接続 | AccessDeniedException |
| シェル・既定シェル・汎用ポート転送 | AccessDeniedException |
| 別のrole session nameから既存SSMセッション終了 | AccessDeniedException |
| 運用ロールの直接RunInstances・TerminateInstances・UserData変更・タグ変更・LT編集 | DryRunでUnauthorizedOperation |
| Automation実行ロールへの直接AssumeRole・文書編集 | AccessDenied |
| UserData等の追加パラメーター | InvalidAutomationExecutionParametersException |
| 別runbook・旧版・実在する未承認の数値版5 | AccessDeniedException |
| バージョン省略・$DEFAULT・$LATEST | 承認版がdefaultの場合も、未承認版をdefaultにした場合も拒否 |
| Targets追加 | 無効パラメーターとして拒否 |
| TargetLocations追加 | 親実行は受理されるがFailed、固定EC2に変化なし |
| Interactive | Waitingで受理、SendAutomationSignalは拒否、実行ステップ0件 |
| 稼働EC2がある状態でTerraform plan | No changes。Automation管理EC2への変更なし |

主な実行ID:

- 同時create: `9cbf97b3-fab0-4285-b944-75e4c367a5f3` / `fbe9aadb-dc3a-4a2f-8851-45cdc6620698`
- 接続中destroy拒否: `33934100-bffa-4916-918b-41ef85a62013`
- 通常destroy: `deb0ab1d-e8ad-4765-b5bd-952d66546e27`
- 同じENIで再作成: `8473158d-9afa-4f64-9884-b3e6615222d8`
- CLI create再利用: `13d812c2-a38f-42cc-8fdc-7043f5ac0bdc`
- 対象外EC2拒否: `4f18394e-2c2d-4d4f-9820-6afd0ed85d3e`
- TargetLocations: `0adc6c7f-1912-4589-9464-c9b9ea001c77`
- Interactive: `df0a3380-5921-4f43-a71d-dd833f7bb251`、ステップ0件のままCancelled。

## 故障注入・撤去結果

隔離コピーのrunbookへ管理者が故障を注入し、共通処理の所有権判定を測定した。

| 条件 | 結果 / 実行ID |
|---|---|
| 既存機の再利用後にcreateステップの出力を失う | 既存機維持。`15e89951-3ed0-4540-a0c1-352b388fb9d4` |
| 新規作成後にcreateステップの出力を失う | 作成実行IDから自分のEC2だけを発見して撤去。`775ffb4f-ebb8-4526-ae61-1eebe8429623` |
| cleanupのTerminateInstancesを一時Deny | 全体Failed、EC2 ID `i-02b099efbdb2eeab6` が出力に残る。`c3bba1ea-475b-4240-8feb-28638490858f` |
| 削除中のcreate | createはFailed、別ENIでの起動なし。その後、正常に再作成。destroy `8736204d-9905-47d4-b12a-f682bc8e1adc` / create `15f54164-0521-4fd5-b048-fad71c4caba4` |

さらに、実RunInstances成功後の応答だけを捨てる検証と、旧EC2の終了確認を120秒遅らせる検証を組み合わせた。

- 応答喪失create `5299dee6-e862-46ba-bf59-1995b7b5fa35`: Success。同じClientTokenのEC2は1台で、SSM Onlineへ到達した。
- 遅延destroy `5cf3f3ad-7e4d-469b-a6ca-7ebd97fb23fd`: Success。旧機 `i-06e8f55b85ad3cfbd` を撤去し、その間に同じENIへ接続した後継機 `i-0e76cfb5bff8eb053` は維持された。
- 故障注入用のコピー文書と一時IAM Denyは削除済み。
- 最後の後継機も通常CLIのdestroy `5b3f997c-f974-466c-9412-af4f7aa01167` で撤去。2026-10-06 10:46:53 JSTにEC2不在・ENI availableを確認した。

これは故障を制御して注入した実AWS試験であり、実際のネットワーク障害を観測した記録ではない。CLIのStartAutomationExecution応答不明・待機期限・出力の秘匿は単体テストでも確認している。

### 常設基盤の最終撤去

確認済みdestroy planを適用し、Terraform管理の30リソースを撤去した。
**2026-10-06 10:53:45 JSTのAWS直接照合で、以下はすべて0件。**

| 残存確認対象 | 件数 |
|---|---:|
| 稼働中EC2・EBS | 0 |
| ENI・VPCエンドポイント・subnet・SG・VPC | 0 |
| IAM role・instance profile | 0 |
| SSM文書・Launch Template | 0 |
| SQSキュー | 0 |
| 実行中Automation・SSMセッション | 0 |
| Terraform state内のmanaged resource | 0 |

`audit.py`は空stateだけでなく、検証VPC・管理タグ・名前prefixからAWSの残存を確認した。完了・失敗・キャンセル済みAutomation履歴とCloudTrail履歴は監査証跡として残る。
本番資源、既存の別検証コード・結果には変更を加えていない。

## ローカル検証

- Terraform fmt / validate: 通常インフラ、bootstrap-access、共通module、隔離fixtureで成功。
- Terraform mock: 通常49、bootstrap-access 13、共通module 2、隔離fixture 1、計65ケース成功。
- Python単体: 運用CLI・既存DLQ CLI 35、ライフサイクル16、計51件成功。AWS応答fixtureをbotocoreの応答schemaと照合。
- Ruff lint / format、git diff --check、CI workflowのYAML解析: 成功。
- 通常インフラの既存service_discovery deprecation警告は残る。今回の実装によるエラーはなし。
- アプリ・DB・frontendの実装を変更していないため、それらのテストは今回の対象外。リモートCIの実行結果はこのローカル記録に含まない。

## 実測で修正した点と制約

1. TerraformのAutomation文書ARNは `automation-definition/...` だった。Start/Get/DescribeDocumentのIAMには `document/...` を明示して修正し、mockもproviderの実形へ合わせた。
2. 起動直後にDescribeInstancesが一時的にNotFoundとなった。新規要求を繰り返さず、同じENI/ClientTokenを再照合して待つよう修正した。
3. 東京1aのt4g.nanoで容量不足が発生した。fixtureを東京1cへ固定し直して検証した。本番のAZや型を自動変更する仕組みは追加していない。
4. AWS CLIは空のParametersオブジェクトを拒否した。createではパラメーター自体を送らないよう修正した。
5. Interactiveは開始自体をIAMで拒否した証拠ではない。実行を進める権限がないためEC2操作は起きない。終了は開始者と同じidentityが必要だったため、テストの当該実行だけにStopAutomationExecutionを一時許可し、同じrole session nameの資格情報を更新してCancelした。権限解除を確認済み。本番ポリシーには追加していない。
6. EC2 DescribeとGetAutomationExecutionの参照範囲は同一アカウント・東京。CLIは固定runbookの必要なID・状態だけを表示するが、CLIの表示制限はIAM境界ではない。
7. destroyのSSM接続確認と直後の新規接続は原子的な排他ではない。撤去前に作業終了を揃える。自動的な時間制限や強制撤去は追加していない。
8. 初回の本番state移管・既存IAM import・本番planは未実施。[初回移行手順](../../aws/bootstrap-access/BASTION_MIGRATION.md)に従い、実リソースと両stateを照合して別途行う。

[AWSの数値版制限](https://docs.aws.amazon.com/systems-manager/latest/userguide/automation-setup-identity-based-policies.html#automation-setup-identity-based-policies-example2)と[Interactive実行仕様](https://docs.aws.amazon.com/systems-manager/latest/userguide/automation-working-executing-manually.html)を参照した。上記の拒否・成功結果は文書からの推測ではなく、このfixtureでの実測である。
