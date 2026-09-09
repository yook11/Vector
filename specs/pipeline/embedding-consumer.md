# EmbeddingConsumer — SQS受信とベクトル生成

Status: Draft（2026-09-09、スライス1のTerraform実装済み・AWS未適用）

業務上の成功・失敗、再配信設定、Taskiqとの併用方針は合意済み。受信adapter・失敗記録・Lambda実行の詳細は末尾の未確定項目に分ける。基盤のTerraform実装と実環境での有効化を区別して記録する。

## Problem

`article.assessed_in_scope`をOutboxからSQSへ送る実装はあるが、受信してベクトル生成を実行するconsumerがない。`EmbeddingConsumer`を追加し、既存の業務処理を再利用してSQS受信から結果保存まで接続する。

現在のTaskiq workerと同時に稼働する期間を許容し、重複配信・並行実行はアプリケーションの処理済み判定と条件付き保存で吸収する。

## Evidence

- [OutboxからSQSへの送信契約](./outbox-sqs-message-contract.md)：イベント形式と送信先。
- [ArticleAssessedInScope](../../backend/app/analysis/assessment/events.py)：イベント種別・バージョン・payload。
- [既存Taskiqタスク](../../backend/app/queue/tasks/embedding.py)：Ready構築、Service呼び出し、失敗処理。現在のtask timeoutは60秒。
- [EmbeddingService](../../backend/app/analysis/embedding/service.py)：AI呼び出しと、ベクトル・成功監査の同一トランザクション保存。
- [EmbeddingRepository](../../backend/app/analysis/embedding/repository.py)：生成済み判定と、embeddingがNULLの場合だけ更新する保存処理。
- [worker起動設定](../../backend/supervisord/analysis.conf)：embedding workerの最大同時実行数は1プロセスあたり10。
- [relay基盤](../../infra/aws/outbox_relay.tf)：Standardキュー、relay Lambda、無効状態のScheduler。consumer・SQS起動トリガーは未実装。
- [Consumer基盤](../../infra/aws/embedding_consumer.tf)：専用サブネット・IAM・SSM経路・DLQ・通知。
- [適用手順](../../infra/aws/README.md#embeddingconsumer基盤の追加スライス1)：bootstrap先行・滞留確認・秘密情報登録・後続検証。
- AWS公式：[SQSとLambdaの接続設定](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)、[同時実行制御](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)、[DLQ](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)。

現状の記載はリポジトリに基づく。AWS実環境の稼働・適用状況は未確認。

## Invariants

- 保存完了とは、ベクトルと成功監査のトランザクションをコミットできたことを指す。
- 「例外なくreturnした」ことだけを根拠にSQSへ成功を返さない。
- 開始時に生成済み、または別の実行が先に保存したことを確認できた場合は対応完了とする。
- 対象記事不存在は失敗とし、処理不要による成功にはしない。
- 初期実装では、想定内か想定外かを問わず処理失敗をSQSへ返す。失敗記録だけでメッセージを処理済みにしない。
- consumer内部で再配信待ちのsleep、独自の段階的バックオフ、DLQへの直接送信をしない。
- 冪等性の業務上の判定対象は`analyzed_article_id`であり、SQSメッセージIDやevent_idだけでTaskiqとの重複を判定しない。
- 通知・ログ・監査の秘匿を維持し、メッセージ本文やSDK例外の自由文を無制限に記録しない。
- 無料枠ゲートを再導入しない。

## 入力と責務

名称は`EmbeddingConsumer`とする。`article-embedding`キューから、以下のイベントを受け取る。実キュー名には既存のname prefixを付ける。

```json
{
  "event_id": "b8969c8e-5c43-4b5e-9867-b20768551666",
  "event_type": "article.assessed_in_scope",
  "schema_version": 1,
  "occurred_at": "2026-09-07T03:00:00Z",
  "payload": {
    "curation_id": 123,
    "analyzed_article_id": 456
  }
}
```

イベント形式は送信契約、payloadは既存`ArticleAssessedInScope`を正本とし、consumer追加のために送信形式を変更しない。SQSが付けるmessageIdとOutboxのevent_idは区別する。

```text
分析結果保存 + Outbox記録
    → relay Lambda
    → article-embedding SQS
    → LambdaのSQS受信機構
    → EmbeddingConsumer
    → Ready構築 → AI呼び出し → ベクトル・成功監査の保存
```

consumer Lambdaは既存Taskiq workerへ依頼を中継せず、自身で業務処理を実行する。既存のReady・AI adapter・Service・Repositoryを再利用し、TaskiqのContext、retry label、brokerの起動処理をSQS入口へ持ち込まない。

## 成功・失敗の契約

| 結果 | consumerの扱い | メッセージの扱い |
|---|---|---|
| ベクトルと成功監査のコミット完了 | 成功 | 対応完了として削除対象 |
| 開始時点ですでに生成済み | 想定内の終了 | 対応完了として削除対象 |
| 他の実行が先に保存し、自分の更新が不要 | 生成済みを確認して想定内の終了 | 対応完了として削除対象 |
| 開始時に対象記事が存在しない | 対象記事不存在の失敗を記録 | SQSへ失敗を返す |
| AI処理中に対象記事が削除され、保存できない | 対象記事不存在の失敗を記録 | SQSへ失敗を返す |
| 入力不正・未対応のイベント、実APIの拒否・429・通信障害・5xx | 失敗を記録 | SQSへ失敗を返す |
| 利用枠枯渇・残高不足・設定不備 | 失敗を記録し、既存の該当する通知を維持 | SQSへ失敗を返す |
| DB処理・コミットの失敗、処理時間上限到達、想定外例外 | 失敗として扱う | SQSへ失敗を返す。強制終了時も成功応答しない |

SQSによるメッセージ削除はLambda連携の成功処理に任せる。consumerから個別の削除APIは呼ばない。

失敗監査自体がDB障害で保存できない場合も成功にはしない。Lambdaの強制終了ではアプリケーションの失敗記録を実行できない可能性があるため、Lambda側の失敗観測も必要とする。

### 既存実装との差分

現在の`EmbeddingRepository.save()`は更新0件を`False`で返し、Serviceは競合として正常終了する。しかし更新0件には「他の実行が保存済み」と「行が削除された」の両方が含まれ得る。

consumerでは両者を区別し、生成済みを確認できた場合だけ対応完了とする。単なる更新0件、失敗handlerによる例外抑止、試行上限到達を成功の根拠にしない。状態確認自体の失敗もSQSへ失敗を返す。

## 実行・再配信設定

| 項目 | 合意値 |
|---|---|
| SQSの種類 | 既存Standardキュー |
| 1起動で処理するメッセージ数（BatchSize） | 1 |
| バッチ待機時間（MaximumBatchingWindowInSeconds） | 0秒 |
| SQSトリガーの最大同時実行数（MaximumConcurrency） | 10 |
| consumer Lambdaの予約済み同時実行数 | 10 |
| 業務処理の時間上限 | 60秒 |
| Lambda全体のタイムアウト | 120秒 |
| SQS可視性タイムアウト | 720秒（12分） |
| DLQへの受信回数上限（maxReceiveCount） | 5 |
| embedding元キューの保持期間 | 4日 |
| DLQの保持期間 | 14日 |

1起動1件であり、関数内で複数記事を並列処理しない。必要に応じて最大10起動が並行する上限で、常時10起動する設定ではない。

業務処理の60秒はReady構築・AI呼び出し・保存を対象とし、Lambda全体の120秒との差は初期化・失敗記録・接続終了の余裕とする。終了処理やSDK設定を含む具体的な時間制御は実装前に確定する。

可視性タイムアウト720秒は、Lambda timeoutの6倍とするAWS推奨に従う。受信時点からの不可視期間であり、失敗時点から12分後の実行予約ではない。期限後に再受信可能となるが、再実行時刻を保証しない。Standardキューの重複配信は引き続き許容する。

maxReceiveCountはSQSの受信回数上限であり、アプリケーションの正確な実行回数を保証する値ではない。SQSのredrive policyでDLQへ移動させ、consumer自身は直接移動しない。DLQからの自動再投入は初期実装に含めない。

### 保持期間と接続経路

- embedding元キューのみ14日から4日に変更し、他工程は維持する。適用前に4日より古いメッセージの滞留を確認する。
- StandardキューはDLQ移動後も元の投入時刻を保持期限の基準とする。元キューの期限切れはDLQ移動ではなく削除となる。
- AI通信は既存の外向きプロキシ経由でGeminiへ接続する。Consumerの送信元と必要な宛先に対応する許可を設ける。
- 記事取得・結果保存はRDSへIAM認証・TLSで接続する。秘密情報はSSMのVPCエンドポイント経由で取得する。
- AI APIキーは既存と同じ値を `/<prefix>/embedding-consumer/gemini-api-key` にSecureStringとして登録し、Consumerの読取権限を専用パスに限定する。APIキーの発行単位は変更しない。
- SSMパラメーターの作成・値の登録は既存の初回構築手順に従いTerraform管理外とする。Lambda向けの取得・初期化処理は別途実装する。

### 送信側設定との区別

relayの120秒timeout、Outboxの150秒lease、最大5回の送信試行、30・120・600・1,800秒を基準とする再送待ちは送信側の契約であり、変更しない。

受信側にOutboxのleaseや送信側バックオフを転用しない。relayが最大10件を一括送信しても、consumerは1件ずつ受信する。

## Taskiqとの併用

- Taskiqによる分析直後のembedding投入と、backfillによる再投入を残したままconsumerを有効化する期間を許容する。
- Taskiq・Lambdaとも同じ分析記事の生成済み判定・条件付き保存に従い、二重保存・成功監査の重複を防ぐ。
- 同時に未生成と判断した場合のAI呼び出し重複は許容する。AI呼び出しを厳密に1回にするための新規ロック・処理済みeventテーブルは追加しない。
- consumerが失敗した後にTaskiqが保存を完了した場合、次のSQS受信では生成済みとして対応完了にできる。
- embedding workerが1プロセスの通常構成では、Taskiq最大10とLambda最大10で合計最大20記事が並行し得る。workerの複数配置・デプロイ時の新旧併存では増え得る。
- AI待機中にDBセッションを保持しない既存の構造を維持する。並列記事数とDB接続数を同一視せず、他の工程も含むDB負荷を有効化時に確認する。
- 処理時間・DB接続数・429を既存の監視で確認し、負荷が高い場合はSQSトリガーの同時実行上限を下げる。

## Non-goals

本文補完・本文整形・投資判定consumerの実装、Taskiqの即時撤去、backfill予算の変更、無料枠ゲートの再導入、送信契約の変更、DLQへの直接送信、DLQからの自動再投入、AI呼び出しのexactly-once保証は含めない。

スライス1はTerraform・テスト・仕様・適用手順の変更までとし、AWS適用・秘密情報登録・Schedulerやconsumerの有効化を行わない。

## 実装順序とDone

1. DLQ、元キューの再配信・保持設定、Consumer専用の権限・ネットワーク・ログを整備する。この段階では受信を開始しない。
2. consumer本体を実装し、成功・失敗・競合・記事不存在の契約をローカルで検証する。
3. Lambda handler・実行イメージ・関数と無効状態のSQS起動トリガーを整備する。
4. 実環境で接続確認後に受信を有効化し、実行・再配信・DLQ移動を検証してTaskiqとの併用を開始する。

Doneは、正常処理・生成済み・競合でメッセージが対応完了となり、失敗が記録され、再配信上限後にDLQへ移り、Taskiqとの重複でDB結果を壊さないことを検証できた状態とする。ローカル実装完了と、AWS上での有効化・検証完了は区別して記録する。

## Implementation

スライス1のTerraformを実装した。embedding元キューのみ保持4日・可視性720秒・受信上限5回に変更し、保持14日の専用DLQを紐付ける。DLQはembedding元キューだけからredriveを許可し、非TLSを拒否する。

Consumer専用サブネットはprimary AZのCIDR index 28とし、appルートテーブルを使用する。専用SGはRDS・proxy・SSMだけに接続し、proxyはGeminiのみ許可する。実行ロールとbootstrapの専用boundaryは元キュー受信・対象DB・専用Geminiパラメーター読取・専用ログ・LambdaのENI管理に限定する。CIのDLQ管理権限とrelayの送信権限は分離する。

DLQ滞留通知は`ApproximateNumberOfMessagesVisible`のMaximum・60秒・1評価期間・1件以上・欠測正常で判定し、ALARM/OK遷移を既存SNSへ送る。自動停止・自動再投入は行わない。障害時は後続のSQSトリガーを手動停止・再開する。

Consumer本体・Lambda・SQSトリガーは未実装。既存Serviceを再利用する際は、更新0件の結果を区別する契約を先に整える。

## Verification

必要な検証:

- 正常時にベクトルと成功監査を同一トランザクションで保存する。
- 開始時の生成済み判定ではAIを呼ばない。
- Taskiqとconsumerの競合では1つの保存結果と成功監査だけが確定する。
- 開始時の記事不存在、処理中の削除、更新0件の再確認失敗を成功にしない。
- コミット後に応答前の終了・接続終了失敗が起きて再配信されても、生成済み判定で吸収できる。
- 不正イベント、API障害、DB障害、60秒の処理上限を失敗として扱う。
- 失敗記録の二次障害で元の失敗を成功に変えない。
- 失敗時に直接DLQ送信やメッセージ削除を行わない。
- AWS上で受信数・同時実行・可視性timeout・redrive policyの設定と動作を確認する。

スライス1の検証（2026-09-09）:

- 本体・bootstrapのfmt check・backend未接続init・validateを実施し、すべて成功した。本体には既存Cloud Mapの`failure_threshold`非推奨警告が残る。
- AWS provider mockテストは本体4件・bootstrap3件が成功した。再配信と送信先の分離、専用ネットワーク、実行権限とboundary、PassRole制約、通知設定を検証した。
- SSOログイン後に`vector-plan`で実環境のread-only planを実施した。本体は18追加・4更新・1削除、bootstrapは1追加・2更新・削除なし。既存キューの再作成はなく、削除はproxyタスク定義の新revisionへの置き換えだけ。他工程のキューは変更なしで、Consumer関数・SQSトリガーの追加もない。relayのSchedulerはDISABLEDを維持する。
- 本体planの更新には既存relay Lambdaの環境変数・image_configが含まれる。planロールの既存`kms:Decrypt`明示DenyによりLambda APIが両項目を返せず、`AccessDeniedException`を返すことを確認した。この2項目は実設定と正確に比較できず、実際の設定差分とは断定しない。権限制約の変更や迂回は行っていない。適用前に既存の承認経路で確認する。
- AWS適用・SSMの実値登録・実通信・通知配送・再配信とDLQ移動の実証は未実施。
- backend・frontendコードを変更していないため、それらの全テストは実施していない。Consumer本体の実装時には`/check`のbackend検証を実施する。

## 実装・有効化前に確定する項目

- SQS eventのvalidation、consumerとLambda handlerのinterface、失敗応答形式（例外伝播か部分バッチ応答か）。
- 記事不存在などの失敗監査の既存分類との対応、event_id・SQS messageId・分析記事IDの観測上の関連付け。
- 業務処理60秒の制御方法、SDK timeout・内部再試行の設定、DB・AIクライアントの生成・終了方法。
- Lambdaのメモリ、SSM取得・キャッシュの実装、専用サブネット・SGをLambdaへ接続する配線。基盤の専用権限を利用し、relayの権限は流用しない。
- 残高不足・設定不備が継続した場合の手動停止・復旧・再投入の具体的な操作手順。DLQ滞留通知は実装済みで、自動停止は行わない。既存holdはSQS起動トリガーを停止しない。

これらの未確定事項は、合意済みの成功・失敗方針と設定値を変更する理由にはせず、該当部分の実装前に仕様を更新する。
