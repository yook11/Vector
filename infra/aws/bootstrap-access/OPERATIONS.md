# 一時踏み台を使ったDLQ運用

## 作業定義

- Problem: ReadOnlyで調査した本人が、既存の一時踏み台を経由して限定運用ロールでDLQを再投入できるようにする。
- Evidence: IAM・SQSキュー・VPCE・踏み台のTerraform、隔離アカウントの[既存実測](../../aws-test/dlq-redrive/VPCE_RESULTS.md)、AWS公式の[再投入権限](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)と[SSM認可](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-examples.html)。
- Invariants: 本番ReadOnlyだけを信頼し、最大1時間、対象5組のARN、固定SQS宛先、TLSと指定VPCE制限、EC2はSSM専用権限を維持する。
- Non-goals: DB権限の変更、CI管理権限の拡大、アプリ変更、既存smoke全体の置き換え、Scheduler/Lambda失敗イベントの独自再投入。
- Done: コード・手順・オフライン／隔離AWSの検証結果がレビュー可能になる。本番適用と実メッセージの再投入結果は別途記録する。

## 責任と認証

管理者で一時踏み台作成 → ReadOnlyで原因調査 → `vector-operations`へAssumeRoleして再投入 → ReadOnlyでConsumerの処理結果確認 → 管理者で踏み台撤去。

| 操作 | 使用する権限 |
|---|---|
| ロール・SSMドキュメントの管理 | `vector-admin`、既存bootstrap-accessのstate |
| 一時踏み台の作成・撤去 | `vector-admin`、既存本番インフラのstate |
| ログ・処理済み状態の調査 | 既存ReadOnly (`default`) |
| 固定SQS接続、再投入・状態確認・停止 | `vector-ops` (`vector-operations`) |
| EC2内のSSM Agent | `AmazonSSMManagedInstanceCore`のみ |

運用資格情報はPCのメモリと子プロセス環境だけで使用し、EC2へ渡さない。
SSMドキュメント`vector-sqs-tunnel`は`SQS東京:443`への転送だけを許可する。
対象EC2は同じアカウント・東京の`vector:session-purpose=sqs-redrive`タグで限定するため、再作成でIDが変わってもIAMを更新しない。
運用ロールにタグ変更、シェル、汎用転送、DB接続、IAM管理は付与しない。DB作業は既存の管理者用手順を継続する。
ロール・SSMドキュメントは常設し、EC2と追加通信規則は`enable_db_bastion=false`で撤去する。

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

1. 最新mainと適用済み状態を照合する。管理者は既存bootstrap-accessのstateとtfvarsを使い、planで送信条件の削除・固定SSMドキュメント・限定SSM権限を確認して適用する。
2. ReadOnlyの既存inline policyに`readonly_operations_assume_statement`があることを確認する。追加が必要なら`NoSecretValues`など既存statementを保持して追加し、Permission Setを再プロビジョニングする。既存の許可を重複追加しない。
3. 運用ロールの存在を確認してから、通常インフラの承認済み反映経路でSQS endpoint policyを更新する。初回は踏み台無効のまま、既存アプリの許可に差分がないことを確認する。
4. ReadOnlyからの引受成功、VectorDeployからの拒否を本番で確認する。過去のmergeやテストアカウントの結果だけで本番反映済みとは扱わない。

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

### 1. 管理者で一時踏み台を作成

既存private runbookに従い、`verify-aws-profile.sh vector-admin`でcallerを確認する。
本番インフラの既存state・tfvarsを使用し、`enable_db_bastion=true`のplanを保存・確認して適用する。
`terraform output -raw bastion_instance_id`の値を操作者へ渡し、SSMがOnlineになるまで待つ。
運用ロールにはTerraform stateの読み取り権限を与えない。

作業中は通常のインフラapplyと時間を重ねない。既定値falseのapplyは踏み台を撤去する。
やむを得ず別applyが必要なら作業を終了して先に撤去する。
途中失敗は作成済みリソース・ログ・残りのplanを照合し、元のapplyを無条件に繰り返さない。

### 2. ReadOnlyで原因と復旧対象を確認

ログ、DLQ件数の変化、イベントと処理済み状態の対応、Consumerの修正反映を確認する。
DLQ件数は未処理件数と同義ではない。対象DLQ全体の移送を開始できる状態か判断する。
標準redriveはメッセージの選別・書き換えを行わない。

### 3. 運用ロールで接続・操作

以下はリポジトリのルートから実行する。`<INSTANCE_ID>`を管理者から受け取った値に置き換える。

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

### 4. ReadOnlyで結果確認、管理者で撤去

タスク完了と移動件数に加え、Consumerの保存成功・再失敗・対象記事の処理状態をReadOnlyで確認する。
キャンセルは移動済みのメッセージを元へ戻さない。DLQが空でも処理成功とは判断しない。
実行中タスクとSSMセッションがないことを確認し、管理者が`enable_db_bastion=false`の削除planを確認して適用する。
EC2・root EBS・一時SG規則・ssmmessages endpointが撤去され、常設SQS endpointと運用ロールが残ることを確認する。

## 検証の区別

Terraform mockとCLI単体テストはCIで実行し、AWSリソースを作成しない。
実AWS試験は別アカウントの[DLQ検証環境](../../aws-test/dlq-redrive/README.md)でダミーだけを使用する。[運用構成の実測結果](../../aws-test/dlq-redrive/OPERATIONS_RESULTS.md)を参照。
本番適用、本番ReadOnlyの入口、本番Consumerの処理成功はそれぞれ別の証拠として記録する。
