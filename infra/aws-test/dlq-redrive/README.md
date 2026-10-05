# DLQ再投入権限の実AWS検証

今回の[運用構成の実測結果](OPERATIONS_RESULTS.md)と、末尾の「運用構成の検証」に手順を記載する。

実行済みの比較結果は[公開経路の検証結果](RESULTS.md)と[VPC経由の検証結果](VPCE_RESULTS.md)を参照。

## 作業定義

- Problem: 本番で再投入時の移動先SendMessageが拒否されるため、IAM条件とキューポリシー条件を別アカウントで比較する。
- Evidence: `infra/aws/bootstrap-access/operations.tf`、`infra/aws/news_pipeline_outbox.tf`、AWS公式のDLQ redrive権限仕様。
- Invariants: アカウント733360597472・東京固定、試験ごとに新規リソース、ダミー本文のみ、各ロールは対応するキュー1組だけに許可。
- Non-goals: 本番ポリシー変更、本番メッセージ再投入、ReadOnly権限セットの入口検証、KMS・Consumerの検証、既存smokeの置き換え（VPC内経路は後述の追加検証で扱う）。
- Done: 実際のAssumeRole後に再投入・直接送信・対象外送信を記録し、試験リソースをdestroyして残存を確認する。

## 構成と比較条件

SSE-SQSのsource/DLQを4組、権限を限定した運用ロール4個、対象外送信の拒否確認用キュー1個を作る。
構築・投入・結果回収・削除にはテストアカウントのWorkloadAdministrator、権限試験には各運用ロールの一時セッションを使用する。
sourceに1件送信し、Receive回数超過でDLQへ移すことで、元キューの情報を保持したメッセージを使う。
各ケースは専用キューに対して1回だけ実行する。失敗時の再試験も新しいrun_idと空のstateを使う。

| ケース | IAMのSendMessage条件 | キューのVPCE制限例外 | 直接送信の期待 |
|---|---|---|---|
| current | CalledViaLast=sqs.amazonaws.com | CalledViaLast=sqs.amazonaws.com | 拒否 |
| queue-called-via | なし・Resource限定 | CalledViaLast=sqs.amazonaws.com | 拒否 |
| queue-via-service | なし・Resource限定 | ViaAWSService=true | 拒否 |
| control | なし・Resource限定 | VPCE制限なし | 許可 |

`current`は比較開始時の旧IAM条件を残すケース名であり、現在の本番向けコードを意味しない。

再投入の成否は実測値であり、候補ケースを成功と決めつけない。controlの再投入が成功しない場合は比較を中断する。
全ケースで対象外キューへの送信は拒否される必要がある。
`vpce-00000000000000000` は公開エンドポイントからの試験専用の比較値で、実在VPCEを作成・検証するものではない。
DLQには本番と同じTLS強制のみを設定する。今回の結果はアカウント固有のSCP、適用済み本番ポリシー、VPC endpoint policyまで保証しない。

## 実行

前提はAWS CLI、Terraform、Python標準ライブラリ。追加パッケージは不要。
このディレクトリを作業場所にし、他のTerraform rootやstateと混ぜない。

```bash
aws sso login --profile vector-test-admin
aws sts get-caller-identity --profile vector-test-admin
terraform init -backend=false -input=false -lockfile=readonly
mkdir -p .local
```

Git管理外の`terraform.tfvars`に、その試験で一意なrun_idを保存する。

```hcl
run_id = "20261004-example"
```

1. `terraform plan -var=phase=seed -out=.local/seed.tfplan` で新規試験リソースだけであることを確認する。
2. `terraform apply .local/seed.tfplan` で作成する。
3. 作成・属性の伝播を待ち、`python3 probe.py seed` でsource → DLQを確認する。
4. `terraform plan -var=phase=verify -out=.local/verify.tfplan` で3つのsource queue policyへのDeny追加だけを確認する。
5. `terraform apply .local/verify.tfplan` 後、SQS属性の伝播に少なくとも65秒待つ。
6. `python3 probe.py verify` で比較結果を`.local/result.json`へ保存する。
7. 成否にかかわらず、下記の削除と残存確認を行う。

IAMは結果整合性のため、引受・観測権限の伝播前に失敗した場合は認可結果とせず、実行前提の失敗として記録する。
DNS失敗・タイムアウト等も拒否成功に数えない。TaskのCOMPLETEDと元本文の戻りを確認し、移動件数の概算値も記録する。概算値は完了直後に遅れて更新されるため、本文到着を確認できた場合の失敗条件にはしない。
検証スクリプトはTerraform apply/destroyを行わない。中断・失敗時にもstateとtfvarsを残して削除する。
再投入中に中断した場合はListMessageMoveTasksで状態を確認し、RUNNINGならCancelMessageMoveTaskで停止する。

```bash
terraform plan -destroy -out=.local/destroy.tfplan
terraform apply .local/destroy.tfplan
terraform state list
aws sqs list-queues --profile vector-test-admin --region ap-northeast-1 \
  --queue-name-prefix vector-test-dlq-<run_id>
aws iam list-roles --profile vector-test-admin --path-prefix /vector-test/dlq-redrive/
```

stateが空であることに加え、今回のrun_idのキュー・ロールがAWSに残っていないことを確認する。
state、tfvars、plan、結果JSONはGit管理外。認証情報はメモリーと子プロセス環境だけで扱い、結果へ出力しない。

## ローカル検証

```bash
terraform fmt -check -recursive
terraform validate
terraform test
python3 -m unittest discover -s . -p 'test_*.py' -v
```

このmock試験はAWS側の認可挙動を証明しない。実AWS結果は実行日時・対象アカウント・適用条件・各ケースの成否・削除確認を分けて記録する。

## 一次資料

- [DLQ redriveの権限とVPC制限例外](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)
- [SQS属性の伝播時間](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SetQueueAttributes.html)
- [完了taskではTaskHandleが返らない仕様](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ListMessageMoveTasksResultEntry.html)

## VPCエンドポイント経由の比較

### 作業定義

- Problem: 公開エンドポイントで拒否された再投入が、指定VPCE経由なら成立するか確認する。
- Evidence: 公開経路の実測結果、SSM Session document・SQS redrive・CLI proxyの公式仕様。
- Invariants: 同じテスト専用アカウント・ダミー本文・キューごとの限定ロールを維持し、EC2はSSM権限だけ、運用資格情報はPC内だけ、TLS証明書検証を維持する。
- Non-goals: 本番への適用、実DB接続、ReadOnly Permission Setの変更、Consumer処理の検証。
- Done: 接続・再投入・直接送信・公開経路拒否・対象外拒否を記録し、今回作成した全リソースの削除を確認する。

空のstateと新しいrun_idで、`terraform.tfvars`に`enable_private_path = true`を追加する。
通常のseed/verify手順により、private VPC、public IPのないEC2、SSM/ssmmessages/SQSのInterface endpointを追加する。
NAT・IGW・SSH・DBは作成しない。費用が発生するため検証直後にdestroyする。

EC2のロールは`AmazonSSMManagedInstanceCore`のみ。運用ロールがPC上で署名したSQS通信をSSM経由で転送する。
専用SSM documentはSQS東京エンドポイントの443だけを転送し、任意のhost・port・shellを指定できない。
運用ロールのStartSessionは試験アカウント・東京の専用タグ付きEC2とこのdocumentだけ、終了は本人のsystem tag条件で許可する。2026-10-04の実測はEC2 ID固定で行い、その後タグ条件へ更新した。

```text
PC上のAWS CLI（一時的な運用ロール資格情報）
  → PC内の固定宛先CONNECT proxy
  → PC内のSession Manager plugin
  → EC2のSSM Agent
  → SQS Interface VPCE
```

追加前提はローカルのSession Manager plugin。Pythonの外部パッケージは不要。
SSMのmanaged nodeがOnlineになってから、verifyを次のように実行する。

```bash
python3 probe.py verify --via-vpce --report .local/vpce/result.json
```

`--endpoint-url`は通常のSQS名を維持し、CLIがSNI・証明書名・Host・署名を揃える。
CONNECT proxyはloopbackだけで待ち受け、SQS東京の443以外を接続前に拒否する。
TLS終端・証明書生成・TLS検証無効化・hosts編集は行わず、認証情報はPC内のメモリと子プロセス環境だけで扱う。
試験用proxy/SSMプロセスはスクリプト終了時に停止し、SSMセッションも終了する。

| ケース | VPCE経由の直接送信 | 公開経路の直接送信 | 対象外キューへの送信 |
|---|---|---|---|
| current | 拒否 | 拒否 | 拒否 |
| queue-called-via | 許可 | 拒否 | 拒否 |
| queue-via-service | 許可 | 拒否 | 拒否 |
| control | 許可 | 今回は未再試験（制限なし） | 拒否 |

再投入は対照ケースの成功を前提に、その他を実測する。IAMのSendMessage条件を外したケースでは、VPCE内からの直接送信も許可される。
VPCEポリシーにも依存アクションを含める。StartMessageMoveTaskだけの許可で十分とは扱わない。
比較matrixではSSMトンネルにcontrol運用ロールを使う。queue-called-viaの引受は現在、試験用調査ロールを経由する。運用構成の試験では接続とSQS操作に同じ運用ロールを使う。
SSM権限の補助試験は、既存backend環境のbotocoreを使う（新規依存の追加なし）。
`backend/.venv/bin/python infra/aws-test/dlq-redrive/ssm_access_probe.py`で、シェル・任意宛先documentの拒否、固定documentの許可、別ロールの終了拒否・本人の終了許可を確認する。
StartSession APIだけを呼び、シェルのチャネルは開かない。想定外に許可された場合も作成したsessionを終了して試験失敗とする。

削除後はSQS・IAMに加え、今回のRunIdタグのVPC/VPCE・EC2（terminated以外）・専用SSM document・instance profileが残っていないことを確認する。

追加の一次資料:

- [Session Managerによるremote hostへのport forwarding](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html#sessions-remote-port-forwarding)
- [Session document schema](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-schema.html)
- [AWS CLIのHTTP proxy設定](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-proxy.html)
- [本人のSSM sessionへの操作制限](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-examples.html#restrict-access-example-user-sessions)

## 運用構成の検証

### 作業定義

- Problem: 本番と同じタグ認可・同一運用ロールの接続／再投入・停止・再作成を検証する。
- Evidence: `infra/aws/bootstrap-access/operations.tf`と、共通実装`infra/aws/scripts/sqs_operations`。
- Invariants: テスト専用アカウント・ダミーだけ、運用ロールの対象1組限定、資格情報はPC内、固定SQS TLS接続を維持する。
- Non-goals: 本番適用・SSO Permission Set変更・Consumer処理の検証。
- Done: 許可と拒否、本文到着、RUNNINGのキャンセル、IAM無変更でのEC2再作成、撤去後の残存ゼロを記録する。

新しいrun_id・空のstateで`enable_private_path=true`を使う。追加する調査ロールは対象1組の属性参照と運用ロールの引受だけを許可する。
queue-called-viaの運用ロールはこの調査ロールだけを信頼する。他の比較ケースは管理者からの引受を継続する。
SSO ReadOnly Permission Set自体の入口は本番反映後の別検証とし、テストで同じPermission Setを新設しない。

1. seed planの新規45件を確認して適用する。`InvalidPolicyDocument`で一部作成になった場合はログとstateを確認し、残りのplanだけを照合して再開する。
2. `terraform output -json fixture`、run_id、作成日時、EC2のroot volume IDを`.local/operations/run.json`へ保存する。資格情報は保存しない。
3. SSM Onlineを確認し、`python3 probe.py seed`で元キュー由来のダミーを各DLQへ送る。
4. `ssm_access_probe.py`でタグなし・異なるタグ・shell・汎用転送・他ロールの終了拒否と、固定document・本人の終了許可を確認する。タグはfinallyで復元する。
5. tfvarsの`phase`を`verify`に変更する。planが3つのsource queue policy更新だけと確認して適用し、SQS設定の伝播を65秒以上待つ。
6. `operations_probe.py verify`で調査ロールの属性参照と再投入拒否、引受後の同一ロールでの接続・status・再投入・本文到着・直接送信の境界・RUNNINGのキャンセルを確認する。
7. `enable_bastion=false`のplanがEC2削除1件だけであることを確認して適用する。`enable_bastion=true`へ戻し、作成1件だけのplanを確認して適用する。
8. 再作成したEC2のroot volume IDをrun.jsonの`root_volumes`へ追記する。Online確認後に`operations_probe.py reconnect`を実行し、前後でEC2 IDが変わり、トンネルポリシーのSHA256が同じで、statusが成功することを照合する。
9. 全DLQの実行中タスクと試験EC2へのActive SSM sessionがないことを確認する。destroy planが今回の試験リソースだけと照合して適用し、`cleanup_probe.py`でstateとAWS上の残存を確認する。

```bash
# リポジトリルートから実行する。
backend/.venv/bin/python infra/aws-test/dlq-redrive/ssm_access_probe.py
python3 infra/aws-test/dlq-redrive/operations_probe.py verify --report infra/aws-test/dlq-redrive/.local/operations/result.json
python3 infra/aws-test/dlq-redrive/operations_probe.py reconnect --report infra/aws-test/dlq-redrive/.local/operations/reconnect.json
python3 infra/aws-test/dlq-redrive/cleanup_probe.py --record infra/aws-test/dlq-redrive/.local/operations/run.json --report infra/aws-test/dlq-redrive/.local/operations/cleanup.json
```

この試験では4ケース比較の`probe.py verify`は実行しない。同じDLQの履歴・ダミーを共有するため、比較matrixを再実行する場合は別runにする。
キャンセル試験用の追加ダミー100件は今回の試験DLQだけに投入し、移動済み・残留分ともキューのdestroyで撤去する。
途中失敗時は報告JSONの成功済み項目と現在のSQS taskを照合し、RUNNINGなら停止してから撤去する。

run.jsonの形式は次のとおり。fixtureはTerraform出力をそのまま格納する。

```json
{"run_id":"<run_id>","fixture":{},"started_at":"<UTC>","root_volumes":["vol-..."]}
```

共通CLIの資格情報解決には[AWS CLI export-credentials](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html)のprocess形式をメモリ内で使用する。
プロファイルの本番固定、stageとARNの固定対応は単体テストで検証し、実AWS試験では同じ操作関数に試験専用のキュー1組を渡す。
