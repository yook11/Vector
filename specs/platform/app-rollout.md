# app rollout — 版の持ち主と対象

Status: 段階1（Terraformが版と有効状態の手入力を手放す）を実装、AWS未適用（2026-09-20）。段階2（rolloutがLambdaを更新する）は未実装。

## Work Definition

- **Problem**: Lambdaの版と有効状態が、コードでもrolloutでもなく「手入力とtfstateの引き継ぎ」で管理されていた。ECSと不揃いで、更新漏れと追加漏れが構造的に起きる。
- **Evidence**: 2026-09-19時点で本番のLambda 14関数が7種類のbackendイメージで稼働していた。`AWS app images`のrolloutはECSだけを更新し、Lambdaは`AWS terraform apply`へ関数ごとのdigestを渡したときだけ版が変わった。関数を足すたびに、digest変数・workflow入力・解決スクリプト・plan／applyの解決手順へ手で追加が必要だった。有効状態の既定値はコード上`false`のまま、本番は全トリガーが有効で、実際の値はtfstateにしか無かった。2026-09-17の`ImageDeleted`障害（[backendイメージの保持](./backend-image-retention.md)）も同じ根から起きた。
- **Invariants**:
  - 切替の各段階で、稼働中のLambdaのイメージ・設定・トリガー状態を変えない。
  - 本番を書き換える経路は承認付きworkflowのままとする。rolloutロールはbackendリポジトリのイメージへの差し替え以外を得ない。
  - rolloutは対象をworkflowへ列挙せず、AWSへ問い合わせる。
  - 途中のどの順序で止まっても、TerraformがLambdaを古い版へ戻す状態を作らない。
- **Non-goals**: 関数単位の版指定・切り戻し入力、smoke環境、bootstrapの列挙整理、共有SGの改名、relayの`for_each`化、取得の旧Taskiq停止、DLQの整理。
- **Done**: app rolloutの1回でECSと全Lambdaが同じイメージになり、検証まで通る。digest・有効状態の変数、workflow入力、解決スクリプトがリポジトリに無い。

## 分担

backendイメージで動くものは、ECSのserviceもLambdaも同じ分担に従う。

| 持ち主 | 持つもの |
|---|---|
| Terraform | 実行設定（メモリ・timeout・環境変数・ネットワーク・権限）と、トリガーの有効状態 |
| app rollout | 版（どのイメージで動くか）。前進も切り戻しも同じ経路で、過去のrelease SHAを指定すれば全体が戻る |

Terraformは版の変更を追わない。ECSは`ignore_changes = [task_definition]`、Lambdaは`ignore_changes = [image_uri]`で同じ型にする。

新規作成時だけは初期値が要る。Lambdaはbackendリポジトリの最新イメージをdigestで参照する（`infra/aws/registry.tf`）。作成後の最初のrolloutで他と同じ版に揃う。

ECSとLambdaで実装が分かれるのは、AWSのAPIが違う箇所だけとする。ロール・workflow・承認・release SHAは1つで、「Lambdaのrollout」という別の仕組みは作らない。

## 有効状態

トリガーの有効／無効はコードで宣言する。止めるときはPRで変更してapplyする。緊急時は管理者がコンソールで止め、あとからコードを追従させる。tfstateからの引き継ぎ（`keep`）は廃止した。

digest未指定なら関数を作らない、という段階投入のスイッチも廃止した。移行期に1本ずつ有効化するための仕組みで、全関数の配置が終わり役目を終えた。

## 切替の順序

1. rolloutロールへLambdaのコード差し替え権限を足す（bootstrap、管理者が適用）。
2. Terraformが版と有効状態の手入力を手放す（段階1）。
3. rolloutがLambdaも更新する（段階2）。

2より先に3を入れない。rolloutが版を進めても、次のapplyがtfstateの古いdigestへ戻してしまうため。

2と3の間は、Lambdaの版が据え置きになる。

## 割り切り

- 空のbackendリポジトリではplanが失敗する。新しい環境では、先にbackendイメージのpushが要る。
- 関数単位の版指定は持たない。要件が出たら、rolloutへ対象を絞る入力を足す。そのときは、ずれた版をいつ揃え直すかも一緒に決める。

## 残課題

- ECSの初期値は今も`var.image_tag`で、workflowがtfstateから引き継いでいる。Lambdaの初期値（最新イメージ）と決め方が揃っていない。rolloutが付け替えるため実害は無い。

## 実施記録

- 2026-09-19: 切替の前に、既存のdigest入力で全14関数をECSと同じイメージへ揃えた。パイプラインに影響しない6関数を先に、本線の8関数を後に更新した。更新後、relayの毎分実行と各ConsumerのErrorsは0、DLQの増加は無かった。
- 2026-09-20: 段階1を実装した。本体のmock plan 38件、bootstrapのmock plan 34件、`backend/tests/scripts` 377件が成功した。変更したworkflowのactionlintは、既知の`queue: max`の警告だけだった。期待する実planは、`count`を外した資源の付け替え23件のみで、add／change／destroyは0とする。
