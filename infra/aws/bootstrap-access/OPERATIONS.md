# 一時踏み台を使ったDLQ運用

## 作業定義

- Problem: ReadOnlyで調査した本人が、固定Automationから一時踏み台を作成・利用・撤去し、限定運用ロールでDLQを再投入できるようにする。
- Evidence: IAM・SQSキュー・VPCE・踏み台のTerraform、隔離アカウントの[既存実測](verification/DLQ_VPCE_RESULTS.md)、AWS公式の[再投入権限](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)と[SSM認可](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-examples.html)。
- Invariants: 本番ReadOnlyだけを信頼し、最大1時間、対象5組のARN、固定SQS宛先、TLSと指定VPCE制限、EC2はSSM専用権限を維持する。
- Non-goals: DB権限の変更、CI管理権限の拡大、アプリ変更、既存smoke全体の置き換え、Scheduler/Lambda失敗イベントの独自再投入。
- Done: コード・手順・オフライン／隔離AWSの検証結果がレビュー可能になる。本番適用と実メッセージの再投入結果は別途記録する。

## 責任と認証

ReadOnlyで原因調査 → `vector-operations`へAssumeRole → 固定手順で踏み台作成 → DLQ再投入 → ReadOnlyでConsumerの処理結果確認 → 運用ロールで踏み台撤去。

| 操作 | 使用する権限 |
|---|---|
| ロール・SSMドキュメントの管理 | `vector-admin`、既存bootstrap-accessのstate |
| 一時踏み台の作成・撤去 | `vector-ops`、承認された数値版Automationのみ |
| ログ・処理済み状態の調査 | 既存ReadOnly (`default`) |
| 固定SQS接続、再投入・状態確認・停止 | `vector-ops` (`vector-operations`) |
| EC2内のSSM Agent | `AmazonSSMManagedInstanceCore`のみ |

運用資格情報はPCのメモリと子プロセス環境だけで使用し、EC2へ渡さない。
SSMドキュメント`vector-sqs-tunnel`は`SQS東京:443`への転送だけを許可する。
対象EC2は同じアカウント・東京の`vector:session-purpose=sqs-redrive`タグで限定するため、再作成でIDが変わってもIAMを更新しない。
運用ロールにタグ変更、シェル、汎用転送、DB接続、IAM管理は付与しない。DB作業は既存の管理者用手順を継続する。
ネットワーク・エンドポイント・専用ENI・IAM・Launch Template・SSMドキュメントは常設する。日常の撤去対象はEC2とroot EBSだけで、時間による自動撤去は行わない。

ReadOnlyの権限はAssumeRole後に引き継がれない。SSMセッション終了権限はAWSが付ける所有者タグと`${aws:userid}`で限定する。
同じロール・同じrole session nameは同じ所有者として扱われるため、この手順は現行の個人運用を前提とする。

## 対象と許容する操作

対象は本番アカウント`222753227567`・東京の次の5組。各通常キューと`-dlq`だけを許可する。

| CLIのstage | 通常キュー |
|---|---|
| acquisition | vector-source-acquisition |
| completion | vector-article-completion |
| curation | vector-article-curation |
| assessment | vector-article-assessment |
| embedding | vector-article-embedding |

- DLQ: `StartMessageMoveTask`、`CancelMessageMoveTask`、`ListMessageMoveTasks`、`ReceiveMessage`、`DeleteMessage`、`GetQueueAttributes`。
- 通常キュー: `SendMessage`。対象全キュー: `GetQueueAttributes`、`GetQueueUrl`。
- キュー側のTLS強制・指定VPCE外からのSendMessage拒否・SQSサービス例外は維持する。
- 指定VPCEから対象通常キューへの直接送信も許可する。再投入専用の権限ではなく、DLQの直接受信・削除も可能である。
- `PurgeQueue`、対象外キュー、通常キューの受信・削除は許可しない。
- SSE-SQSを前提とし、KMS権限を追加しない。CLIも暗号化設定が変わっていたら停止する。

Scheduler失敗DLQとLambda非同期失敗キューはSQS標準再投入の対象に含めない。

## 初回導入

初回の状態移行・管理者適用は[Bastion移行手順](BASTION_MIGRATION.md)に従う。
ReadOnlyの既存inline policyに`readonly_operations_assume_statement`があることを確認する。
既存の`NoSecretValues`などを保持し、Permission Set全体を置き換えない。
本番適用と実メッセージの再投入は個別に結果を記録する。

AWS CLI v2、Python 3、Session Manager pluginをPCに用意し、profileを設定する。

```ini
[profile vector-ops]
role_arn = arn:aws:iam::222753227567:role/vector-operations
source_profile = default
role_session_name = vector-dlq-operations
duration_seconds = 3600
region = ap-northeast-1
```

ReadOnlyの信頼条件はPermission Setの割当先全員に適用される。利用者を増やす場合は運用ロールの委譲範囲も見直す。
既存のVectorDeployに運用ロール専用の引受許可が残っている場合は、ReadOnlyでの成功確認後にそのstatementだけを除去する。

## 毎回の運用

### 1. 運用ロールで一時踏み台を作成

```bash
aws sso login --profile default
infra/aws/scripts/verify-aws-profile.sh vector-ops
python3 infra/aws/scripts/bastion.py create
python3 infra/aws/scripts/bastion.py status
```

CLIは対象アカウント・東京・実callerを確認し、公開された数値版を明示して固定手順を開始する。
既存の正常な踏み台があれば同じIDを返す。作成中はSSM Onlineを最大10分待ち、削除中・停止中・想定外の機体は新規作成に切り替えない。
資格情報の期限切れ・CLI終了・待機期限到達でもAutomationは続行する。表示した実行IDで追跡する。

```bash
python3 infra/aws/scripts/bastion.py status --execution-id <EXECUTION_ID>
```

開始応答を受け取れず実行IDが不明なら、表示された要求IDと開始時刻を管理者に渡し、CloudTrailのStartAutomationExecutionとSSM実行履歴を照合する。
同じ操作を新しい要求として繰り返さない。運用ロールにTerraform stateの読み取り権限は与えない。

### 2. ReadOnlyで原因と復旧対象を確認

ログ、DLQ件数の変化、イベントと処理済み状態の対応、Consumerの修正反映を確認する。
DLQ件数は未処理件数と同義ではない。対象DLQ全体の移送を開始できる状態か判断する。
標準redriveはメッセージの選別・書き換えを行わない。

### 3. 運用ロールで接続・操作

以下はリポジトリのルートから実行する。`<INSTANCE_ID>`を`bastion.py create/status`で取得した値に置き換える。

```bash
aws sso login --profile default
infra/aws/scripts/verify-aws-profile.sh vector-ops
python3 infra/aws/scripts/dlq-redrive.py status --stage assessment --instance-id <INSTANCE_ID>
python3 infra/aws/scripts/dlq-redrive.py start --stage assessment --instance-id <INSTANCE_ID>
python3 infra/aws/scripts/dlq-redrive.py status --stage assessment --instance-id <INSTANCE_ID>
python3 infra/aws/scripts/dlq-redrive.py cancel --stage assessment --instance-id <INSTANCE_ID> --task-handle <TASK_HANDLE>
```

`status`は本文を受信せず件数と直近10タスクを表示する。`start`は対応する通常キューへ毎秒1件で移送し、TaskHandleを出力する。
実行中・停止処理中タスクがあれば新規開始を拒否する。`cancel`は対象DLQのRUNNINGタスクとの照合後にだけ呼び出す。
送信先ARN、レート、接続先サービス、profileの変更引数は設けない。

CONNECT proxyはPCのloopbackだけで待ち受け、SSMを通して既存SQS VPCEへ接続する。
TLS検証と通常のSQSホスト名を維持し、SSO・STS・SSM通信はSQS用proxyへ通さない。
CLIは資格情報・本文・AWSの生エラーを表示せず、終了時にproxy・plugin・SSM sessionを片付ける。

開始のタイムアウトや中断では結果が不明な可能性がある。自動再試行は行わず、`status`で確認する。
接続の終了はSQSタスクの停止ではない。移送を止めるときは`cancel`を明示する。
終了済みタスクではTaskHandleが返らない場合があるため、source・destination・開始時刻・状態でも照合する。
資格情報の期限切れや終了失敗時は、表示されたSessionIdを管理者が確認して終了する。

### 4. ReadOnlyで結果確認、運用ロールで撤去

タスク完了と移動件数に加え、Consumerの保存成功・再失敗・対象記事の処理状態をReadOnlyで確認する。
キャンセルは移動済みのメッセージを元へ戻さない。DLQが空でも処理成功とは判断しない。
SSMセッションを終了してから、明示した機体を撤去する。SSM接続が残っていれば手順は拒否する。

```bash
python3 infra/aws/scripts/bastion.py destroy --instance-id <INSTANCE_ID>
python3 infra/aws/scripts/bastion.py status
```

撤去完了は対象EC2の終了・root EBS削除・そのEC2からのENI解放まで確認する。
後続の別作成が同じENIを取得しても、その後継機を削除しない。通常操作に強制撤去はない。
削除失敗時は実行IDとEC2 IDを残し、管理者が実リソース・SSM実行履歴を照合してから復旧する。
EC2がない間もInterface endpointと専用private IPv4など常設基盤は残り、endpoint料金は続く。

## 検証の区別

CIの`Terraform / operations tests`ではAWSリソースを作成せず、本番コードの次の重要な条件を検証する。

- Terraform mock: 固定起動設定、許可する操作と対象、SSM・SQS・DBの接続境界。
- Python単体: 対象外操作と重複実行の拒否、既存機・後継機の誤削除防止、結果不明時の再試行抑止、失敗時のID保持と接続の後片付け。
- ローカルソケット試験: SQSだけへのCONNECT許可、他の宛先・HTTPメソッド・過大なヘッダーの拒否、TLSのバイト列を変更しない転送。

実AWS検証は別アカウントのダミー資源で実施し、検証資源を撤去済み。単発検証のTerraform環境・実行スクリプト・それら専用のテストは維持せず、実測結果と制約を残す。

- [公開経路での権限比較](verification/DLQ_PUBLIC_RESULTS.md)
- [VPCエンドポイント経由の権限比較](verification/DLQ_VPCE_RESULTS.md)
- [運用ロール・固定トンネル・DLQ操作](verification/DLQ_OPERATIONS_RESULTS.md)
- [固定Automationによる作成・撤去](verification/BASTION_AUTOMATION_RESULTS.md)

IAM・通信経路・作成撤去処理を変更した際は、PR作成前に影響する成功条件と拒否条件を別アカウントで再確認し、実施日・対象コード・結果・残存資源の確認を記録する。過去の実測は変更後のAWS動作を保証しない。
本番適用、本番ReadOnlyの入口、本番Consumerの処理成功はそれぞれ別の証拠として記録する。
