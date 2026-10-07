# 工程別backfillのAWS構成（スライス3）

> 2026-09-20: digest入力と`*_state`入力は廃止した。以下は構築時の記録で、現在の扱いは[app rollout](../platform/app-rollout.md)を参照する。
> 2026-10-07: 分析3工程の定期起動を外した。「分析3工程の定期起動の停止」を参照する。

## Work Definition

- **Problem**: 実装済みのcuration・投資判定・embedding backfillをAWSへ配置し、定期起動できる構成を用意する。
- **Evidence**: `app.lambda_handlers.backfill` の設定・入口、既存Outbox relayのprivate接続・実行権限、completionのstate引き継ぎ、bootstrapの権限制限とTerraformテスト。
- **Invariants**: 工程別送信権限、RDS IAM接続、重複許容、30分間隔とoffset、初回有効・更新時は版と稼働状態を維持、既存経路を継続する。
- **Non-goals**: AWS適用・実環境確認、旧経路撤去、カスタムmetric出力、アプリ本体・DB・consumerの変更。
- **Done**: Lambda・Scheduler・接続権限・CI配線をコード化し、mock構成テストと設定解決テストで検証してPRにする。

## 配置と起動

| 工程 | 関数名 | handler末尾 | UTC cron |
|---|---|---|---|
| curation | `${name_prefix}-curation-backfill` | `curation_handler` | なし |
| assessment | `${name_prefix}-assessment-backfill` | `assessment_handler` | なし |
| embedding | `${name_prefix}-embedding-backfill` | `embedding_handler` | なし |
| completion | `${name_prefix}-completion-backfill` | `completion_handler` | `cron(15,45 * * * ? *)` |

handlerのパッケージは `app.lambda_handlers.backfill`。backend ECRイメージをarm64・512MB・120秒・予約同時実行数1で利用する。既存relayのprivate subnetとsecurity groupを再利用し、RDSは `vector_backfill` のIAM認証、自工程のSQS送信だけを許可する。環境変数はproduction、DB接続、IAM認証、自工程のキューと有効設定のみで、AWSリージョンはLambdaの標準環境変数を使用する。

Schedulerはflexible window OFF、入力 `{}`。Scheduler配送再試行とLambda関数エラー再試行はそれぞれ0回、最大イベント有効期間は両方60秒。Lambda非同期実行設定とScheduler実行権限の作成後にscheduleを作成する。基盤側の重複配送は引き続き許容し、未完了記事は次回のDB抽出で拾い直す。

新しい起動要求用DLQとカスタムmetricは追加しない。CloudWatch Logsは既存の保持期間を利用する。

## イメージと稼働状態

Terraform変数は各工程の `<工程>_backfill_image_digest` と `<工程>_backfill_enabled`。digestなしではLambda・非同期実行設定・scheduleを作成しない。実行ロール、ロググループ、schedule groupは既存relayと同様に準備する。

plan/applyは `scripts/resolve-backfill-images.py` でroot moduleの `backfill` リソースの `index_key` を検証し、`backfill.auto.tfvars.json` を生成する。壊れたstate、重複工程、digest形式以外のイメージ、Lambdaのないscheduleは失敗させる。置換済みのdeposed instanceと他moduleのリソースは現行値として使わない。初回配置の途中でLambdaだけ作成された場合は、そのイメージを引き継いでscheduleを作成する。

| CI指定 | 結果 |
|---|---|
| digest空欄・state `keep`、未配置 | 起動リソースを作成しない |
| 初回digest指定・state `keep` | 有効で配置 |
| 既存あり・state `keep` | digest未指定部分と稼働状態をstateから維持 |
| state `disabled` | Lambdaを残し、該当scheduleを停止 |
| state `enabled` | 指定または既存digestがあれば有効化、なければ失敗 |

applyの明示digestはbackend ECRに存在することも確認する。state取得・解決・ECR確認がすべて成功した場合だけ設定ファイルを配置する。既存consumer・relayの解決処理は変更しない。

## bootstrap

2026-09-22に段別のロール6本を段共通の2本へ統合した。実行ロール `${name_prefix}-backfill-lambda` とSchedulerロール `${name_prefix}-backfill-scheduler` を、それぞれ専用のboundaryへ固定する。実行boundaryは `vector_backfill` でのRDS接続、4工程の `article-*` キューへの送信、4工程のロググループ、関数コードからのENI操作拒否を持ち、Scheduler boundaryは4関数の起動だけを許す。schedule groupは `${name_prefix}-backfill` 1つに4 scheduleを収容し、Schedulerの信頼元はSourceAccountとこのgroupに限定する。工程を足すときは `backfill_stages` に加えるだけで、ロール・boundary・CIの許可表は増えない。

CIにはbackfillだけのLambda・非同期実行設定・schedule管理権限を持たせる。段別の旧boundaryと旧schedule／groupの管理許可は、本体の切替後に撤去した。段共通boundaryのTerraformラベルは、旧アドレスがstateから消えた後に `moved` で `backfill_lambda_boundary`／`backfill_scheduler_boundary` へ戻した。PassRoleのサービス対応とapp rolloutからの分離は維持する。統合の方針は[IAMロールの概念別統合](../platform/iam-role-consolidation.md)を参照する。

## 検証の分担

- Terraform mockテスト: 初回配置・停止、handler/cron/queue対応、再試行・実行上限、IAM・boundary・endpoint・ECR、既存経路への非干渉。
- スクリプト単体テスト: state維持、明示更新・停止、不正入力、実workflow shellの原子的な設定配置。
- 既存 `local_tests`: 配送・再投入・実DB接続管理を引き続き所有する。

CIの既存Terraform検証jobで、bootstrap-access・bootstrap・本体のbackendなし初期化、validate、mockテストと設定解決スクリプトのテストを実行する。

## 後続のAWS適用

1. bootstrap権限を先に更新する。
2. 3工程のdigestを指定し、scheduleを有効で配置する。旧経路は継続する。
3. 工程別ログで定期起動を確認し、監査と対応させてSQS受付からconsumerのDB確定まで確認する。
4. Lambdaの起動・エラー・実行時間、Schedulerの配送失敗を確認する。curationの期限切れ整理最大200件を含め、120秒が妥当か確認する。
5. 問題があれば対象工程を `disabled` にして旧経路を継続する。

カスタムmetric送信設定と旧経路の撤去は実環境確認後の別スライスとする。

## 分析3工程の定期起動の停止（2026-10-07）

- **Problem**: curation・投資判定・embeddingのbackfillは、Taskiqで再試行を使い切った記事を拾うために入れた。今は失敗した記事がSQSのDLQに残るため、定期起動は次の無駄だけを生んでいる。再試行しないと決めた記事を7日間送り直す。通常の再配信と重なってAIを重ねて呼ぶ。原因が直らないまま送り直した複製がDLQに溜まる。
- **Evidence**: 2026-09-14〜10-06の送り直しはcuration 3,200件・投資判定4,003件・embedding 71件で、10/3以降は0件だった。本文上限超過の1記事を420回送り直した。DeepSeek残高不足の3日間は4,003件を送り直し、失敗が19,912回、投資判定のDLQが3,942件増えた。応答不正の1記事は複製で95回失敗した後に成功し、複製18件がDLQに残った。Outboxの送信停止は直近30日で0件だった。
- **Invariants**:
  - 3工程のLambda・実行ロール・非同期実行設定・環境変数は変えず、scheduleだけを外す。
  - 補完のbackfillは30分間隔の定期起動を続ける。
  - Consumer・relay・DB・backfill本体のコードは変えない。
- **Non-goals**: backfill本体とLambdaの撤去、DLQアラームの削除、分析Consumerの呼び出し回数のアラーム、Outboxの再開操作、IAM・boundary・bootstrapの変更、補完のbackfillの変更。
- **Done**: 補完のscheduleだけを残す構成をTerraformで定義し、mockテストで確かめる。適用後、3工程のbackfillの定期起動が止まり、補完のbackfillが動き続けていることを確認する。

### 止まった記事の扱い

- Consumerで失敗が続いた記事はDLQに残る。原因を直したあと、DLQのredriveで元のキューへ戻す。DLQのメッセージは最初の送信から14日で消える。

## 一次資料

- [Terraform Lambda非同期実行設定](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_function_event_invoke_config)
- [Terraform Scheduler設定](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/scheduler_schedule)
- [Lambda非同期呼び出しのエラー処理](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)
