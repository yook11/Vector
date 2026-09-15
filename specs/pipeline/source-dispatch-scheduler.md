# ソース取得依頼の投入 — EventBridge Scheduler / Lambda / SQS

Status: 一部実装（2026-09-15）。ステップ1の入力検証・取得依頼生成、ステップ2のSQS送信・失敗分の再送、ステップ3のLambda入口を実装した。AWS設定の作成・適用は未実装。以下の「提案」は未確定であり、実装済みとして扱わない。

## Problem

ニュース取得の最前段にある定期投入を、EventBridge Schedulerが共通のLambdaを起動し、Lambdaがcadenceに応じたソースの取得依頼をSQSへ直接送る構成へ移す。Scheduler、対象選定、送信、取得Consumerの責務と、投入失敗時の回復境界を定義する。

## Evidence

- [現行スケジュール](../../backend/app/queue/schedule.py): HIGHは15分、MEDIUMは1時間、LOWは6時間ごとのcron。
- [対象選定](../../backend/app/collection/sources/dispatch.py): DBの有効なソースをコード登録と照合し、コード定義のcadenceで選別する。未登録・不正なソースは個別の拒否結果にする。
- [現行投入タスク](../../backend/app/queue/tasks/acquisition.py): 定期3経路と管理者の手動経路があり、ソースごとにTaskiqへ投入する。現在の一部投入失敗を捕捉して継続する動作は、新Lambdaの成功条件の根拠にはしない。
- [現行取得入力](../../backend/app/queue/messages/collection.py): DBのソースIDとコード定義の検索キーであるソース名を渡す。
- [Outbox送信契約](./outbox-sqs-message-contract.md): アプリのevent_idとSQSのMessageIdを区別し、再送でも同じevent_idを維持する。ただし本工程は保存済み記事の完了イベントを配送するOutbox relayではなく、取得を依頼する処理である。
- [既存AWS構成](../../infra/aws/outbox_relay.tf)と[既存監視](../../infra/aws/alerting.tf): Lambda・Scheduler・SQS接続とdispatch_runの監視がある。実装パターンの参照元であり、その権限・再試行設定の流用を確定するものではない。

## 合意済みの構成

```text
EventBridge Scheduler（HIGH / MEDIUM / LOW の3スケジュール）
  → 共通の投入Lambda（cadenceを入力として受け取る）
  → 対象ソースを選定
  → 1ソースにつき1件の取得依頼をSQSへ直接送信
  → 取得Consumerがソースからニュースを取得
```

- Schedulerが「いつ」、投入処理が「どのソース」、取得Consumerが「どう取得するか」を担当する。
- 投入Lambdaではニュース取得を行わない。SQSへの取得依頼を、取得完了を示すイベントとして扱わない。
- 届いた依頼は普通に処理する。同じ取得依頼IDについて処理済みと確認できた場合はスキップする。
- 厳密な一度だけの実行は要求しない。重複配送を許容し、まれな二重取得を防ぐためだけの大きな仕組みを追加しない。
- 遅れた依頼は実行時点のソースを取得する。予定時刻時点の内容を再現するものではない。依頼の古さだけを理由にアプリで破棄・統合しない。
- SQSを巡回して送信済みか確認する専用処理は設けない。投入の結果は送信側、滞留はキュー、取得結果はConsumerで観測する。
- 同じLambda実行内では、失敗・受付不明の依頼だけを再試行し、送信成功済みの依頼は再送しない。個別再試行の上限に達しても失敗が残ればLambdaをエラー終了する。上限到達によるエラー終了・Lambda自体の停止のどちらも、AWSによる全体再実行・再送を許容し、同じ取得依頼IDで重複に対応する。送信進捗の永続化は今回導入しない。
- 失敗イベントを発行して別処理へ連携する実装は今回行わない。ログ・監視による通知と、AWSの最終失敗記録の具体的な設定は別途決める。

## 配送と失敗の境界

| 境界 | 成功が示すこと | 失敗を扱う主体 |
|---|---|---|
| Scheduler → Lambda | Lambdaの非同期起動依頼が受け付けられた | Schedulerの配送再試行・最終失敗記録 |
| ソースごとのSQS送信 | その取得依頼についてSQSの受付を確認できた | 同じLambda実行内で失敗・受付不明の依頼だけを上限まで再試行する |
| Lambdaの実行 | 送信対象すべてのSQS受付を確認できた。対象なし・拒否の扱いは別途定義する | 個別再試行上限後の残存失敗・全体障害はLambdaの非同期実行再試行・最終失敗記録 |
| SQS → 取得Consumer | 取得依頼の処理が完了した | 取得ConsumerとSQSの受信・再配信契約 |

SchedulerはLambdaを非同期に呼び出すため、Lambda内の送信完了までは確認しない。Lambdaの再試行にも回数・イベント保持時間の制限があり、最終的なSQS配送成功を無条件に保証しない。[AWSの起動仕様](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html)、[Lambdaの再試行仕様](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)

- 個別の送信失敗・応答不明を投入成功に変換しない。同じ実行内では個別再送を行い、上限後も失敗・受付不明が残る場合はLambdaをエラー終了する。この場合もLambda自体の停止後と同じく、成功済み分を含む全体再送を許容する。
- SQS受付後に応答が失われる場合がある。受付有無を別途照合せず再送できるよう、同じ依頼の識別子を維持する。
- 10ソース中1件の送信が失敗した場合、成功した9件はそのままConsumerの処理へ進め、失敗した1件を同じ実行内で再試行する。Lambda自体が途中停止した場合は対象を選び直して再送し、成功済み分が再送されることを許容する。
- まとめて送る場合はHTTP成功だけで判定せず、個別の成功・失敗を確認する。[SQS SendMessageBatch](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessageBatch.html)
- 最終失敗は記録・通知する。Schedulerの配送失敗とLambda内の投入失敗の両方を対象とし、SchedulerのDLQだけで両者を回収できるとは扱わない。
- アプリによる古い依頼の破棄を設けないことと、AWSの再試行期限・SQS保持期間は別である。期限切れの記録と回復方法も明示する。

## 項目別の決定事項と残る提案

### 1. スケジュールと対象選定

提案: 現行のUTC固定cronに対応するAWSのcron式を使い、Flexible Time WindowはOFFとする。LOWはUTCの00・06・12・18時（JSTの09・15・21・03時）であり、JSTの00時起点へ黙って変更しない。時刻表の管理元と旧cronを停止する手順を決める。

確定（2026-09-13）: 初回・再試行とも、実行時にDBを照会し、有効なソースから指定cadenceに合うコード登録済みソースを選定する。有効状態はDB、cadenceと取得定義はコードを参照する。同じ予定回の再試行中に設定が変わった場合、対象が増減することを許容する。個別失敗の再試行では失敗した対象について状態を再確認し、有効な別ソース全体を再送する意味にはしない。選定後の無効化を取得Consumerでどう扱うかは別途決定する。

初回一覧を固定して再現することは要求しない。送信進捗や失敗対象の一覧を、停止後の個別再開のために永続化しない。Lambda全体の再実行では、最新の状態で選定した対象を送信する。

対象0件、個別ソースの未登録・名前不正、DB照会失敗を区別する。提案は「対象なしは正常終了」「個別拒否は理由を記録して有効な対象を送信」「DB照会失敗は実行失敗」。全件拒否を通常の対象なしと同じ監視結果にしない。

### 2. メッセージ契約と取得依頼ID

確定（2026-09-13）: SchedulerからLambdaへの入力は`cadence`と`scheduled_at`の2項目とする。`scheduled_at`は「そのcadenceの予定回の時刻」であり、実際にLambdaが起動した時刻ではない。Schedulerはソースの一覧や取得依頼IDを作らず、Lambdaがこの入力で対象ソースを選び、取得依頼を組み立てる。

```json
{
  "cadence": "high",
  "scheduled_at": "2026-09-13T01:00:00Z"
}
```

LambdaからSQSへ渡す取得依頼の基本項目は`request_id`、`cadence`、`scheduled_at`、`source_id`とする。ソース名の併記、schema versionや外側のメッセージ形式はConsumerとの接続時に決める。

確定（2026-09-13）: 取得依頼IDはハッシュを使わず、`cadence/予定実行時刻/ソースID`の文字列とする。例は`high/2026-09-13T01:00:00Z/123`。同じ3項目ではScheduler再配送、Lambda再実行、SQS再配信でも変えない。異なるcadence・予定回・ソースは別IDとする。予定実行時刻は元の予定回を示し、実際の起動・送信時刻に置き換えない。スケジュール名・ARNは取得依頼IDの構成要素にしない。

ID生成は共通の関数1か所が所有し、「cadence・予定実行時刻・ソースIDを受け取り、取得依頼IDを返す」という契約にする。IDを生成・再生成するすべての箇所はこの関数を使い、各呼び出し元で文字列連結や時刻整形を重複実装しない。関数は現在時刻・乱数・DB・AWSに依存しない。

正規形はcadenceの既定の小文字値、UTCの`YYYY-MM-DDTHH:MM:SSZ`、正のソースIDの先頭ゼロなし10進表記を`/`で連結したものとする。タイムゾーン付き時刻はUTCへ揃える。入力のタイムゾーン欠落・秒未満の扱いは境界検証で明示し、黙ってローカル時刻を補完したり異なる時刻を丸めて同じIDにしたりしない。

Consumerは受信した`request_id`を処理済み確認に使う。整合性確認のためにIDを再生成する場合も同じ生成関数を使う。共通関数の具体名・配置はソース取得依頼の責務に合わせて実装時に決める。

Consumerはこの取得依頼IDで処理済みを確認し、処理済みなら取得をスキップする。IDの付与だけで重複実行が防げるとは扱わず、処理済み記録の保存方法は第4項で決める。厳密な同時実行排除は要求しない。

SQSのMessageIdとLambdaの実行IDを業務上の取得依頼IDには使わない。Schedulerのexecution-idも呼び出し試行ごとに変わる。予定実行時刻とスケジュールARNはSchedulerのコンテキスト属性で渡せる。[AWSのコンテキスト属性](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-schedule-context-attributes.html)

既存Outboxの5項目形式を採用するか、取得コマンド用の形式を定義するかは、既存受信部品との適合を確認して決める。形式を合わせるためだけにOutbox行を追加しない。

### 3. 部分失敗、再試行上限、回復

確定（2026-09-13）: 同じLambda実行内の再試行は、ソースごとの取得依頼を単位とする。個別送信が失敗しても他ソースへの送信を続け、成功済み依頼はそのまま進める。失敗・受付不明の依頼だけを同じ取得依頼IDで上限回数まで再試行する。

確定（2026-09-13）: Lambda自体の停止・timeout等では、AWSの非同期実行再試行を使い、元のcadence・予定実行時刻で関数の先頭から実行し直す。最新のDB状態で対象を選び直して送信し、先行実行で送信成功した依頼の再送も許容する。同じ取得依頼IDについてConsumerが処理済みと確認できればスキップする。

同じ実行内の失敗一覧はメモリで保持し、停止位置・送信進捗の永続化、未完了分の厳密な引き継ぎ、専用の再試行キューは今回導入しない。再試行回数・保持時間には上限があり、全ソースの投入完了は保証しない。将来、停止後も成功済み分を除いて再開するなどの厳密な保証が必要になった場合は、永続的な進捗管理等を別の仕組みとして設計する。Consumerの処理済みID記録は、送信進捗の管理とは別の責務として第4項で決める。

確定（2026-09-13）: 個別再送の上限後も失敗・受付不明が残った場合は、その結果をログ・メトリクスで表し、Lambdaをエラー終了する。正常戻り値に失敗情報を入れるだけの終了にはしない。AWSの設定された上限内で全体再実行につなげ、成功済み分を含む再送を同じ取得依頼IDで扱う。個別送信失敗の発生時点で他ソースへの送信を直ちに打ち切る意味ではない。

確定（2026-09-15）: アプリは初回込み最大3回、再送ラウンド前に一様乱数で0〜1秒・0〜2秒待つ。回復可能な通信・サービス障害だけを再送し、未知のサービスエラー・応答不正・想定外の例外は同じ実行内で再送しない。残り実行時間が少ない場合の終了方法はLambda接続時に決める。

以下の値と手順を実装時に確定する。

- 確定: SendMessageで個別送信し、同時実行数は1。接続timeoutは3秒、応答待ちは5秒、SDKはstandard・total_max_attempts=3。
- Lambda実行timeout、メモリ、同時実行数。ソース数と送信上限時間で全対象を処理できることを確認する。
- Schedulerの配送再試行回数・保持時間とDLQ。
- Lambda非同期実行は関数エラー時の再試行を最大2回、イベント保持期限を6時間とする（設定適用は後続）。保持期限は関数エラーを6時間再試行する意味ではない。上限後の記録方法は未決定。AWSのDLQ・失敗時の送信先を採用するかは未確定であり、独自の失敗イベント発行を今回の必須実装にしない。送信先を採用する場合は記録失敗も観測する。
- 最終失敗の通知先と復旧手順。失敗理由と識別可能な依頼をログ・監視で把握する。途中停止時に正確な未送信一覧が残ることは保証しない。手動復旧では元のcadence・予定実行時刻で全体を再実行する案とし、具体的な実行手順は未決定。

### 4. Consumerとの接続条件

以下は送信側だけでは完結しないため、Consumer側の実装契約で確定する。

- 取得依頼IDの処理済み記録の保存先、記録タイミング、保持期間。受信しただけで処理済みにしない。保持期間は再配信・手動再実行の可能期間と整合させる。
- 投入後に無効化・削除されたソース、コード登録がなくなったソースの受信時の扱い。
- 異なる依頼による同一ソースの同時取得を許す範囲。厳密な重複排除とは分けて判断する。
- SQSキュー種別（Standardを提案）、保持期間、Consumerの実行基盤、可視性・受信回数・取得失敗時のDLQ。

既存の処理済み記録がこの要件を満たすとは仮定しない。DB schemaの追加が必要なら、具体的な変更案を提示してから進める。

### 5. 配備、監視、切替

- Lambda入口・設定層・資源ライフサイクルはステップ3で実装した。対象選定に不要な取得クライアント・AI・Redisを初期化しない。AWSへの配備・通常経路の有効化は後続とする。
- DBへの読み取り接続、SQS送信、ログ・メトリクス、失敗記録に必要なIAM・ネットワーク経路を定義する。既存relayの権限やVPC endpoint policyで新Lambdaがそのまま動くとは仮定しない。
- cadenceごとの対象件数・拒否件数・送信確認件数・投入完了・失敗を記録する。予定どおりの投入完了が途絶えた場合の監視を設け、既存dispatch_run監視との接続を決める。
- Consumerの受信準備後に旧定期投入を停止し、新Schedulerを有効化する順序とロールバック手順を決める。切替中の実行中タスクと投入済みメッセージを考慮する。
- 管理者の手動取得を今回切り替えるかを明示する。提案は定期投入だけを今回の切替対象にし、手動取得を変更する場合は別途入力・権限・ID生成の契約を定義する。

## Invariants

1. 同じ取得依頼の再送で識別子を変えず、重複実行を許容する前提を保つ。
2. 送信結果が不明・失敗の場合に、全対象を投入できたと報告しない。
3. 起動受付、SQS受付、ニュース取得完了を混同しない。
4. 元の依頼時刻を再実行時の現在時刻で置き換えない。
5. 依頼の古さだけでアプリ側の取得依頼を破棄・統合しない。
6. 送信確認のために業務キューを巡回・消費しない。
7. 未決事項や提案を、合意済み・実装済みの契約として扱わない。
8. 同じLambda実行内の個別再試行には送信成功済みの別依頼を含めず、個別再試行上限後のエラー終了・Lambda自体の停止による全体再実行では再送を許容する。
9. 取得依頼IDの生成・正規化は共通関数に集約し、各呼び出し元に別の生成規則を持たせない。

## Non-goals

- 本文補完・AI分析など後続工程の再設計、取得アルゴリズムの変更。
- 失敗イベントの発行と、そのイベントを受けた別処理による自動復旧。
- 厳密な一度だけの実行、初回対象集合の完全な再現、投入確認用のSQS巡回処理。
- 送信進捗の永続化、取得依頼のOutbox化、未完了分を引き継ぐ専用キュー。
- ソース別の個別Scheduler、DB schema・認証認可の変更の実施。
- この仕様整理でのコード実装、AWS適用、旧経路の停止。

## Done

本整理は、合意済み構成・提案・未決事項・実装の検証条件を区別して記載できれば完了とする。実装着手前には担当スライスに関係する未決事項を確定する。

後続実装では、cadence別選定、対象0件と拒否・DB障害の区別、個別の送信結果、同じ実行内での失敗分だけの再送、個別再送上限後の残存失敗によるエラー終了、応答不明時とLambda全体再実行時のID維持、停止後の対象再選定と全体再送、上限後の未完了の可視性を検証する。Scheduler入力からConsumer受信までの形式一致、失敗記録、監視、切替も各スライスに適したローカル・AWS検証で確認し、未実行と実稼働確認を区別する。

## ステップ1：入力から取得依頼の生成

実装範囲は入力検証、共通ID生成、既存の対象選定から取得依頼への変換までとする。送信・再試行・Lambda入口・AWS適用・Consumerの処理済み管理には接続していない。

- [取得依頼の契約](../../backend/app/collection/sources/acquisition_request.py)の`SourceAcquisitionSchedule`は`cadence`と`scheduled_at`を受け取り、予定時刻をUTCへ正規化する。タイムゾーンなし・非ゼロの秒未満を拒否し、マイクロ秒未満の文字列表現も解析前に拒否する。
- `build_acquisition_request_id`がID生成を所有する。`SourceAcquisitionRequest`は予定回の`schedule`と`source_id`を持つ不変の依頼型で、`request_id`は生成時に共通関数で計算してフィールドへ保存する。呼び出し元が別のIDを渡す構築口を作らず、`to_message()`で予定回を展開して合意した4項目を返す。
- `SourceDispatchService.select(cadence)`がDBと取得定義を照合する。選定対象と対象外の理由はこの既存の確認結果に属し、DB照会の失敗は例外として伝える。
- `SourceAcquisitionSchedule`は繰り返し設定全体ではなく1回分の予定を表し、`schedule.create_request(source_id)`でソース1件の取得依頼を生成する。呼び出し側は確認済みの対象ソースにこの振る舞いを適用して依頼一覧を作る。依頼一覧に選定診断を混ぜた結果型や、そのためだけの中継クラスを設けない。
- 呼び出し側が毎回対象を確認し、その対象から依頼一覧を作る。対象外の理由は確認結果から別途扱うため失われない。対象が空なら依頼一覧も空になる。監査書き込み・送信成功記録・進捗保存は行わず、既存SQL・Taskiq経路は変更していない。

検証の所有先:

- [単体テスト](../../backend/tests/collection/sources/test_acquisition_request.py): IDの同一性と識別、補完・丸めが必要な時刻の拒否。DB照会は新しい関数の責務に含めず、中継クラスの例外伝播だけを確認していたテストは削除した。既存の選定障害の検証は維持する。
- [ローカルテスト](../../backend/local_tests/acquisition/test_source_acquisition_requests.py): migration適用済みDBと実際の選定・依頼生成を接続し、DBの現状態に対応する取得依頼一覧を検証する。対象外の理由は既存の確認結果側で確認する。同じ選定シナリオのモック単体テスト・別DB統合テストは追加しない。
- 後続の送信・Lambda接続はこのローカル経路を拡張し、正常経路を別テストへ複製しない。AWS実配送はこのステップの検証対象外とする。

検証結果（2026-09-13）: 取得依頼一覧と選定診断を分離した実装で、Ruff lint・format check、全単体6,866件、DB統合1,434件、ローカル82件が成功した。新規追加は単体6ケースと実DBのローカル1シナリオ。ステップ1は完了とし、SQS送信・AWS実行は未実装・未検証。

追加修正（2026-09-14）: 予定回を取得依頼が保持する形へ変更した。IDは依頼生成時に確定して保存し、参照時には再計算しない。既存の単体6ケースを予定回から依頼を生成する経路へ更新し、ローカル1シナリオでは`to_message()`による4項目の形式を検証する。重複するテストは追加しない。

```python
schedule = SourceAcquisitionSchedule.model_validate(event)
selection = await dispatch.select(schedule.cadence)
requests = tuple(schedule.create_request(source.id) for source in selection.targets)
```

追加修正後の検証結果（2026-09-14）: Ruff lint・format check、全単体6,836件、DB統合1,434件、`make test-local`の82件が成功した。別ワーキングツリーでは認証DB準備用の既存フロントエンド依存をロックファイルから導入して再実行した。今回の新規テスト追加はなく、既存の単体6ケースとローカル1シナリオを更新した。


## ステップ2：取得依頼のSQS送信

Problem: 予定回から選んだ取得依頼をSQSへ送信し、成功済みを除いた再送と最終失敗を扱う。
Evidence: ステップ1の予定回・依頼型、既存SourceDispatchService、app.http.failureの通信分類、既存OutboxのSQSコード対応表を確認した。Outboxの送信型はUUID・バッチ送信を前提としており、今回の文字列ID・単件送信へ流用しない。
Invariants: 全対象の初回送信を先に行い、成功済みは同じ実行内で再送しない。再送は同じ依頼・同じJSON本文を使い、未送信を正常結果へ置き換えない。DB障害と外部キャンセルは伝播する。
Non-goals: DB schema、依存、旧Taskiq、既存Outbox契約、Lambda入口、AWS適用、Consumer重複管理、進捗永続化、独自の失敗イベント、通知を変更・追加しない。
Done: 合意した配送シナリオと境界の検証、既存チェックが通り、結果とAWS未接続の範囲を記録する。

### 実装契約

- SQSの送信・受付結果・SDK例外の解釈は`app/aws/sqs`が所有する。取得依頼側は`SourceAcquisitionSender`という送信契約に依存し、収集側の`SqsSourceAcquisitionSender`が変換した`SourceAcquisitionSendError`を受けて`acquisition_retry`で再送を判断する。投入工程・再送判断・送信アダプターは`collection/article_acquisition`に置く。SQS固有の分類を解釈するのは送信アダプターだけとし、工程には再送の扱いと安全な診断情報を渡す。収集側ではSDK例外やAWSコードを解釈せず、SQS側は収集ドメインをimportしない。既存のOutbox送信とLambda受信の配置・契約は変更しない。
- `SourceAcquisitionDispatcher.dispatch(schedule)`は呼び出すたびにDBで対象を選び、`schedule.create_request(source.id)`で不変の依頼を生成する。正常時の戻り値はNone。対象0件はSDKクライアントを生成せず終了し、除外理由は既存の選定診断に残す。
- `app.aws.sqs.message_sender.SqsMessageSender.send(body)`はSendMessageを1回呼ぶ（SDK内部の最大3試行を含む）。応答は`SqsSendResponse.from_response(raw_response)`で形式を検証した送信応答へ変換し、`response.verify_body(body)`で送信本文と照合する。送信応答の型は非空のMessageIdと32桁の十六進MD5を持つ不変の型とし、応答不正と本文不一致を区別する。SQSレコードによるConsumerへの処理依頼とは別の通信上の概念である。
- 命名はSDKへ本文を渡す`SqsMessageSender`と、SendMessageの応答を表す`SqsSendResponse`に分ける。応答型は送信する業務メッセージの形式を定義せず、検証後もSender内部で扱う。
- `app.aws.sqs.message_sender.create_sqs_client(session, region)`は接続3秒・応答待ち5秒、standard・初回込み最大3回を設定する。キューURLは投入側から1つ渡す。既存クライアント設定は変更しない。
- `open_acquisition_sender`をsender_factoryとして注入し、内部で`open_sqs_message_sender`を利用する。クライアント生成時のSQS例外も工程側の例外へ変換する。クライアントの生成・通信・解放はスレッドへ移し、イベントループを塞がない。外部キャンセル時は進行中のSDK操作を回収してから解放し、キャンセルを伝播する。
- `app.aws.sqs.errors.SqsSendFailure`は発生事実のみを保持し、`SqsSourceAcquisitionSender`が工程側の`AcquisitionSendFailure`へ変換する。`should_retry_acquisition_send`は変換済みの再送区分と試行回数だけで判断する。共通のclassify_botocoreで通信障害を分類する。
- 通信timeout・DNS・network_io・protocol_violation・通信分類内のunknown、SQSのthrottled・service_unavailableを再送する。proxyはstatusなし・429・5xxを再送し、TLSとその他のproxy拒否は再送しない。
- 既知のSQS拒否コードはHTTP statusより優先する。未分類5xxはサービス障害、それ以外の未知コードは調査対象とする。設定・資格情報不足、既知の拒否、応答不正・本文不一致、想定外の例外はアプリ内で再送しない。
- 初回込み最大3回を依頼ごとに適用し、再送ラウンド前に一度だけ0〜1秒・0〜2秒待つ。待機と乱数は注入可能。再送中は対象を選び直さない。
- 最終失敗は`UnsentAcquisitionRequest`にID・試行回数・失敗事実を保持し、`source_acquisition_send_failed`ログを出して`AcquisitionDispatchError`を返す。クライアント初期化で止まった場合は全対象を試行0回の未送信として記録する。
- ログは取得依頼ID・試行回数・失敗分類・サービスコード・例外型・通信失敗理由に限定する。本文・Queue URL・資格情報・SDK自由文・生の例外チェーンを出さない。ログ・closeの通常例外は先行する送信結果を上書きしない。

### 検証の所有先

- 既存の実DBローカルシナリオを送信まで拡張した。Engadgetの初回成功とTechCrunchのサービス障害→応答timeout→成功を接続し、同じ本文・IDで失敗分だけを再送すること、DB変更後の対象再選定、cadence・登録状況による除外を確認する。
- 上限到達と、再送対象外の失敗があっても他のソースを完了させる経路を追加した。後者は既知拒否を5xxより優先、未知コード、応答欠落、本文不一致を代表ケースとする。SDK境界のみ差し替え、送信アダプターと再送処理を実行する。
- 単体では配送シナリオを複製せず、取得依頼側でDB障害の伝播、AWS側でcloseと診断の失敗による成功結果の維持・通信中のキャンセルと資源回収を検証する。既存3ケースの配置を責務に合わせ、新しいケースは追加していない。既存のID・時刻検証は維持する。
- SDK内部の再試行アルゴリズム、AWS実配送・IAM・Lambda全体再実行は今回のテストでは検証しない。

検証結果（2026-09-15）: SQSの責務を分離したmain上の実装で、Ruff lint・format check、全単体7,166件、`make test-integration`の1,475件、`make test-local`の138件が成功した。ステップ2での追加は配送を重複検証しない単体3ケースと、既存ローカル1シナリオの拡張・失敗系5ケース。今回のarticle_acquisitionへの配置変更・工程例外への変換後も、Ruff lint・format、単体7,166件（not integration）、統合1,475件、ローカル138件が成功した。全testsの直接実行は既定DB未起動で停止したため、単体と専用DBの統合実行に分けて検証した。既存テストの配置と参照先を更新し、再送対象外と調査対象の変換結果を既存ローカルケースで確認した。ケースの追加はなく、クライアント生成失敗の工程例外への変換は補助チェックで確認した。AWS実配送とLambda接続は未実装・未検証。

命名整理後の検証結果（2026-09-15）: `SqsMessageSender`・`SqsSendResponse`への改名後、Ruff lint・format、単体7,166件、DB統合1,475件、ローカル138件が成功した。テストケースの追加はなく、送信・応答検証・再送の振る舞いは維持した。


## ステップ3：予定回をLambda入口へ接続する

Problem: Schedulerが渡す予定回をLambda入口で検証し、既存の取得依頼投入へ接続する。
Evidence: 既存の予定回型・投入処理・SQS送信、DB接続生成・IAM署名器、Lambdaの設定と資源管理を確認した。`vector_collect`は既存migrationで`news_sources`のSELECT権限を持つ。既存Relay専用の接続名や記事取得用HTTP設定の初期化は流用しない。
Invariants: 元の予定時刻と依頼IDを維持する。不正入力では設定・接続資源を生成せず、設定不足では投入を始めない。全件成功または対象0件だけを正常終了とし、DB障害・未送信を成功に変換しない。通常の解放・診断失敗は確定済みの結果を変えない。
Non-goals: AWSリソース作成・適用、DB schema・権限・依存変更、既存OutboxやTaskiqの変更、Consumer重複管理、進捗保存、失敗通知を追加しない。
Done: Lambda入口から実DB・実投入処理へのローカル接続と、入口固有の失敗・資源回収の検証、既存チェックが通る。

### 入口と接続契約

- `app.lambda_handlers.source_dispatch.handler.handler(schedule_input, context) -> None`が予定回を受け取る。入力は既存の`cadence`・`scheduled_at`だけで、SQSレコードやイベントEnvelopeへ包まない。既存`SourceAcquisitionSchedule`で検証してから設定を読む。
- `SourceDispatchSettings`は`DatabaseConnectionSettings`を継承する。`DATABASE_URL`・`AWS_REGION`・`SQS_SOURCE_ACQUISITION_QUEUE_URL`を要求し、`DB_IAM_AUTH`は既定true・false拒否とする。`ENV`は既存区分、既定productionとし、既存の本番TLS・IAM URL内パスワード禁止を維持する。`.env`やアプリ全体の設定は読まない。
- DB接続先は`vector_collect`を指定する。実行ごとにRDS署名クライアントと専用Engineを所有し、既存IAM providerを接続する。Engineは`NullPool`、接続・コマンドの制限各5秒、接続名`vector-source-dispatch`。SDKクライアント生成・解放をスレッドへ移し、RDS初期化中のキャンセルでも生成結果を回収する。
- `open_source_dispatcher`が既存の`SourceDispatchService`・`SourceAcquisitionDispatcher`を組み立てる。SQSは既存送信部品を遅延生成し、対象0件ではSQSクライアントを作らない。再送ループ・本文・ID生成を入口へ複製しない。
- 入口の失敗は`SourceDispatchLambdaError`に変換し、`input`・`settings`・`resources`・`dispatch`の段階と例外型だけを保持・記録する。元の自由文・例外連鎖を表示せず、通常の例外は必ずエラー終了へつなぐ。未送信依頼の詳細ログは既存投入処理だけが所有し、入口では複製しない。
- キャンセル・プロセス停止を通常例外へ変換しない。Engine・RDS解放の通常例外は安全な診断だけを残し、処理結果を上書きしない。SQSの解放契約は既存部品が所有する。

### 検証の所有先とAWS接続

- `local_tests/acquisition/test_source_acquisition_requests.py`の既存6ケースを実際の同期Lambda入口から呼ぶ形へ拡張する。実設定・IAM接続・対象選定・SQS送信を使い、AWS署名器・SQS SDK呼び出し・待機だけを差し替える。ログの捕捉時のみ共通ログ初期化を止める。再呼び出しで新しい接続と最新DB状態を使い、対象0件ではSQSクライアントを生成しないことも確認する。
- 単体は入口の入力拒否、設定不足・IAM条件違反、DB障害、資源準備失敗時の回収、解放・診断失敗による結果の維持、RDS初期化中のキャンセルに限定する。DB障害の既存1ケースを入口へ移し、同じ配送シナリオや時刻・ID検証を追加しない。
- Lambdaの関数エラー時の最大2回再実行・イベント保持6時間は合意済みだが未適用。Lambda制限時間、Scheduler・キュー・IAM・ネットワーク設定、実AWSでの動作確認は後続とする。
- [Lambda公式の入口仕様](https://docs.aws.amazon.com/lambda/latest/dg/python-handler.html)に従い、同期の2引数入口から非同期処理を実行する。[SQLAlchemyのイベントループ間の接続共有に関する説明](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops)に沿い、実行ごとのEngineと`NullPool`で接続を持ち越さない。

検証結果（2026-09-15）: ステップ3は完了。Ruff lint・format、入口固有の単体9ケース、全単体7,174件、DB統合1,475件が成功した。ローカルテストは取得工程の`local_tests/acquisition/`へ移し、全139件の収集と`make test-local`の139件成功を確認した。既存6配送ケースを入口へ拡張し、DB障害1ケースを移設したため単体の純増は8件。AWS接続・実配送・設定適用は未実施。
