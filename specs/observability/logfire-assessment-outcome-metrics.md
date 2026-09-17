# Assessment processing outcome metrics

## 現行経路

AssessmentはSQS / LambdaのConsumerで実行する。旧Taskiqのtask・retry・hold・失敗handler・spanは撤去する。

`vector.assessment.processing_outcome`とCloudWatchの`processing_outcome`は、次の結果を1件ずつ計測する。

| result | 計測境界 |
|---|---|
| `in_scope` | Serviceが対象内結果・成功監査・後続Outboxを同一transactionでcommitした後 |
| `out_of_scope` | Serviceが対象外結果・成功監査を同一transactionでcommitした後 |
| `failed` | Consumerが処理例外を受け、失敗後処理を開始したとき |

Consumerの失敗計測にはDB障害も含む。旧Taskiq専用の`infra_error`分類は使わない。
Ready拒否・既処理skipは計測せず、Consumer初期化時のカテゴリー不足・DB接続障害は入口の初期化失敗として扱う。

Logfire属性は`result`だけとし、記事ID・入力値・例外本文を載せない。CloudWatchでは`stage=assessment`と`result`をdimensionとする。

## 集計

- 成功率: `(in_scope + out_of_scope) / (in_scope + out_of_scope + failed)`
- 失敗率: `failed / (in_scope + out_of_scope + failed)`
- 対象内比率: `in_scope / (in_scope + out_of_scope)`

分母が0の窓は比率を計算しない。CloudWatchの既存失敗率alarmは維持し、旧Redis Streamの停止検知・観測alarmは撤去する。

失敗監査の分類・秘匿・元例外の伝播は[Assessment Consumer仕様](../pipeline/assessment-consumer.md)に従う。
失敗後処理では計測・監査・provider枯渇通知を独立して試み、通常の二次障害で元例外を置き換えない。

## 検証

結果と属性の契約は通常の単体テスト、保存と成功監査・Outboxの原子性はDBテスト、通知とカテゴリー初期化はローカルテストで確認する。
