# CurationConsumer — 分析可能な記事の完成イベントによる本文整形

Status: スライス1のAssessment／EmbeddingにおけるReady拒否の受信完了を実装・検証済み（2026-09-13）。Curationの通常経路移行と3工程すべての統一は後続スライスとし、AWSへの適用は未実施。

## Problem

分析可能な記事が完成したことを契機に、CurationをOutbox・SQS・Lambdaで実行する。取得段階で本文まで揃った場合と、本文補完段階で揃った場合は、同じ業務上の事実として一種類のイベントを発行する。

新経路のAI・DBなどの実行失敗はAssessment／Embedding Consumerに揃える。一方、Readyを作れないと確定した場合は、3工程とも理由を記録して受信完了とし、メッセージを削除して再配信を止める。現行Assessment／Embeddingの欠損時の失敗伝播も、このタスク内の専用スライスで変更する。

旧Taskiqのstage hold、日次投入上限、失敗時の記事削除を新経路へ持ち込まない。救済経路は別タスクとし、通常経路の移行に集中する。

## Evidence

- [取得サービス](../../backend/app/collection/article_acquisition/service.py)と[本文補完サービス](../../backend/app/collection/article_completion/service.py): 記事の保存とOutbox記録を同じトランザクションで確定する。現在のイベント種別は発行元ごとに分かれている。
- [ReadyForCuration](../../backend/app/analysis/curation/domain/ready.py): DB上の記事の存在、Signal／Noiseの保存済み状態、タイトル・本文の制約を判定する。本文の上限は200,000文字。
- [CurationService](../../backend/app/analysis/curation/service.py)と[Repository](../../backend/app/analysis/curation/repository.py): Signal／Noiseの保存、成功監査、Signal時のOutbox記録を所有する。現在のService戻り値はIDまたはNone。
- [Assessment仕様](./assessment-consumer.md)と[Embedding仕様](./embedding-consumer.md): 正常終了と失敗伝播、借用するAIクライアント、呼び出し単位の資源管理、SQS部分バッチ応答の参照元。
- [Outbox送信契約](./outbox-sqs-message-contract.md)と[relay実行部](../../backend/app/lambda_handlers/outbox_relay/execution.py): 保存済みイベントの検証・配送と、単一イベント種別の配送入口を提供する。
- [Curationタスク](../../backend/app/queue/tasks/curation.py)、[救済タスク](../../backend/app/queue/tasks/backfill.py)、[ECS設定](../../infra/aws/ecs.tf): 旧Taskiq経路が残り、本番向け定義では3工程の救済が有効になる構成。
- [既存Curation保存テスト](../../backend/tests/analysis/curation/test_curation_service_audit.py): Signalと対応Outboxの保存、Noise・保存競合時のイベント非作成、Outbox失敗時の原子性を検証している。

既存コード・仕様は現状の証拠として扱う。移行後の方針は本仕様とユーザーの合意を優先する。AWSの現在の稼働状態は確認していない。

## 合意した範囲

- 起動イベントは一種類。取得と本文補完が同じイベントを発行する。
- Curationの通常経路をOutbox → SQS → Lambdaへ移す。
- 新経路はstage hold・日次投入上限を持たず、失敗時に記事を削除しない。
- Readyを作れないと確定した場合は、Curation・Assessment・Embeddingすべてで理由を記録してメッセージを削除し、再配信しない。この統一は今回の実装範囲に含める。
- 救済は別タスクで扱い、当面は既存Taskiqで動作していてよい。
- 保存済みの旧イベントへの対応は保留し、本仕様の実装条件に含めない。
- 過去の未処理検索・メトリクスの整理や互換維持を今回の目的にしない。

以下のイベント名・正常終了型・配置・時間設定は、この方針に対する具体的な仕様案とする。

## 全体フロー

```text
取得時に分析可能な記事を保存 ─┐
                            ├─ 同じ記事完成イベントをOutboxへ保存
本文補完で分析可能な記事を保存 ┘
  → EventBridge SchedulerがCuration向けrelay Lambdaを定期起動
  → relay → article-curation SQS → Curation Lambda
  → Ready構築 → GeminiによるCuration → 結果の確定
      ├─ SIGNAL: article.curated_signalを同時にOutboxへ保存
      │    → 既存Assessment relay → Assessment SQS → Assessment Lambda
      └─ NOISE: 後続イベントなし
```

Schedulerはrelayを起動する。記事完成イベントの発行はDB保存境界、配送はOutbox relay、記事単位の実行はSQSを受けるLambdaが所有する。

## 記事完成イベント

- 名前の案は`AnalyzableArticleCreated`、event_typeは`article.analyzable_created`、schema_versionは1とする。「分析可能な記事の作成が確定した」という事実を表す。
- 共通契約はcollection配下に置き、取得側・本文補完側の両方が参照する。一方の工程専用イベントをもう一方が借用する構造にはしない。
- payloadは正の整数の`analyzable_article_id`だけとする。本文・タイトル・発行経路ごとのIDは載せず、ConsumerがDBから必要な事実を取得する。
- 外側のevent_id・event_type・schema_version・occurred_at・payloadは既存のOutbox送信形式に従う。送信時にID・時刻を再生成しない。
- 取得段階で分析可能な記事を新規保存した場合、または本文補完によってその保存に成功した場合に、同じイベントを記録する。業務行・成功監査・Outboxを同じトランザクションで確定する。
- 本文不足のまま保存した場合、記事保存の競合で後続に進まない場合、保存が失敗した場合は、このイベントを確定しない。本文補完の起動方式は今回変更しない。
- 「一種類」はイベントの意味と契約の統一を指す。配送は重複し得るため、ConsumerはDBの処理済み状態で冪等に終了する。
- 新しい発行処理では旧2種類との二重発行を行わない。新relayとConsumerが受け付けるのは新契約だけとする。
- 保存済みの旧イベントの変換・再発行・削除・互換受信は実装しない。

## Consumer・Serviceの責務

- Lambda入口は設定・秘密情報・DB・AIクライアントの準備、SQS構造とイベントの検証、Consumer呼び出し、応答、資源の終了を所有する。
- `CurationConsumer`は検証済みイベントを受け、DBから事実を取得し、Readyを構築してServiceを実行する。Taskiq Context・Redis・SQSメッセージ形式には依存しない。
- Ready構築に必要な事実は1回の照会で取得し、取得セッションを閉じてからAI処理へ進む。監査の記事情報はDBで確認した事実に基づき、イベント由来IDから存在を補完しない。
- Serviceは既存のSignal／Noise判定・翻訳タイトル・要約の意味を維持し、結果・成功監査・必要なOutboxの確定を所有する。Consumerから後続イベントを再発行しない。
- 新ConsumerからAssessmentのTaskiqタスクを投入しない。後続は確定したOutboxだけを入口にする。

## 全工程共通のReady拒否と受信完了

Readyを作れないと確定した場合、同じイベントを再配信しても次の処理へ進めないため、そのイベントの処理を終了する。理由を記録し、SQSのメッセージを削除する。記事データ自体は削除しない。

| 工程 | Ready拒否として受信完了にする例 | 処理済みとして受信完了にする状態 |
|---|---|---|
| Curation | 対象記事の欠損、本文200,000文字超過、タイトル・本文のReady入力制約違反 | SignalまたはNoiseが保存済み |
| Assessment | 対象Curationの欠損、翻訳タイトル・要約のReady入力制約違反 | 対象内または対象外の判定が保存済み |
| Embedding | 対象分析済み記事の欠損、埋め込み入力テキストのReady入力制約違反 | ベクトルが保存済み |

- 各工程のReady側は、不変の`ReadyBuildRejected`値で拒否理由とDB由来記事IDを返す。`ReadyBuildBlockedError`と別の拒否結果を併存させず、ConsumerはReady側から受け取った同じ値を監査へ渡し、受信完了結果として返す。工程間で扱いを揃えるための汎用Consumerや共通基底クラスは要求しない。
- 処理済みの理由もReady側の同じ拒否値で伝え、Consumerが既存の処理済みCompletionへ対応付ける。処理済みの監査・成功計測を追加しない。
- Readyモデル生成時の入力検証エラーだけを`INPUT_INVALID`へ対応付け、入力制約やEmbeddingテキスト生成ルールを変えない。
- 拒否監査は`REJECTED`と理由コードを記録し、本文・入力値・検証例外を保存しない。対象欠損では記事IDを補完せず、入力不正ではDB由来IDを使う。通常の監査障害は安全なログとaudit-dropped計測へ退避し、拒否を再配信に戻さない。
- Lambda入口はこの結果のmessageIdを`batchItemFailures`へ含めない。SQS連携の成功応答によってメッセージを削除させ、ConsumerからSQSの削除APIを呼ばない。
- Ready拒否ではAI・結果保存・成功監査・後続Outboxを実行しない。終了理由を記録し、分析成功や処理済みとは区別する。
- Ready拒否を失敗応答にしないため、この理由での自動再配信・DLQ送りは行わない。既存の監査上のretryabilityから配送判断を導出しない。
- 本ルールは対象の状態・内容からReadyを作れないと確定した場合に適用する。DB取得障害や想定外例外を一括してReady拒否へ変換しない。
- 現行Assessment／Embeddingの対象欠損を例外として返す動作は変更対象であり、本仕様の共通ルールを優先する。

## Curationの処理完了と受信完了

Serviceの正常終了は`CurationCompletion`で表し、`kind`を次の3種類とする。Consumerはこれらに加えて前節の理由付きReady拒否を返せる契約とする。AIが返すSignal／Noise、DB上の処理完了、Ready拒否による受信完了は区別する。

| 結末 | 根拠 | AI・後続イベント |
|---|---|---|
| `SIGNAL` | Signal結果・成功監査・Outboxをcommitできた | `article.curated_signal`を記録済み |
| `NOISE` | Noise結果・成功監査をcommitできた | 後続イベントなし |
| `ALREADY_CURATED` | 開始時のSignal／Noise保存済み確認、または保存時の既存一意制約による競合スキップ | 開始時ならAIを呼ばず、追加の成功監査・Outboxなし |

`SIGNAL`には保存したcuration_idを持たせる。IDまたはNoneだけの戻り値で、Noise保存・処理済み・失敗を混同しない。Repositoryの保存ID／競合時None／例外の契約は維持し、Serviceが正常終了型へ対応付ける。

記事欠損・本文上限超過などのReady拒否は、Serviceの実行前にConsumerが受信完了へ対応付ける。`SIGNAL`・`NOISE`・`ALREADY_CURATED`のいずれかに置き換えたり、Serviceの成功結果を合成したりしない。

## 失敗の扱い

- AI入力・応答、provider、DB、期限切れ、想定外例外を失敗として伝播する。確定したReady拒否は前節の受信完了として扱う。既存のRecoverable／TerminalやTaskiqの再試行回数を新Consumerの制御に使わない。
- AIによるコンテンツ拒否も例外として扱い、記事DELETEを実行しない。新Consumerは旧`CurationFailureHandler`へ委譲しない。
- 失敗理由・元の原因を保持し、監査の分類と再配信の制御を分ける。監査上のretryabilityでSQSの成功応答へ変換しない。
- 成功保存・成功監査・Outbox・commitの失敗は正常終了に変換しない。同じトランザクションの未確定結果はロールバックする。
- 失敗監査を別のトランザクションで試み、必要なprovider枯渇通知などの後処理は既存Consumerの責務分担に揃える。旧メトリクスの区分・値の互換維持は要求しない。
- 監査・ログ・終了処理の通常の二次障害で元例外を上書きしない。本文・秘密情報・SDK例外の自由文を配送診断へ追加しない。キャンセルやプロセス終了を正常終了にしない。
- Lambda入口は個別イベント入力不正・Consumer実行失敗のmessageIdだけを`batchItemFailures`へ返す。処理済み・Ready拒否のmessageIdは含めない。初期化やバッチ構造の不正はバッチ全体の失敗として伝播する。
- 新経路に独自の再試行、stage hold、日次投入上限、Redis接続を追加しない。個別失敗の再配信と最終的なDLQ移動はSQS側の設定に任せる。

## 資源管理・実行設定

- Curatorは準備済みGeminiクライアントを借用する。モデル・プロンプト・結果schemaの変更は目的に含めない。
- SSMからの秘密情報取得、RDS IAM・TLS接続、Engine・session factory、AIクライアントの生成終了をLambda呼び出し単位で管理する。AI応答待ちにDB接続・トランザクション・ロックを保持しない。
- Geminiのクライアント管理・通信設定は既存のprovider共通部品を使う。具体的な準備・終了の共通化は、実装開始時に存在するAssessment／Embeddingの構成に合わせる。別作業中のライフサイクル共通化の完了を必須条件にしない。
- 初期設定案は既存Consumerに揃え、業務処理60秒、Lambda120秒、SQS可視性720秒、受信バッチ1件、最大同時実行10、5回受信後に専用DLQへ移動とする。業務期限の外で失敗後処理を行う。
- Curation専用Consumer・relay・実行権限・必要な通信経路・秘密情報の参照先・DLQを定義する。既存Curationキューの資源を利用し、relayの起動は既存と同じ1分間隔を初期案とする。
- インフラ実装時に公式ドキュメントと既存設定を確認し、時間・同時実行・権限の整合性を検証する。新しい依存パッケージやDB schema変更は前提にしない。

## 旧Taskiqとの境界

- 通常経路の切替では、取得・本文補完からの`curate_content.kiq()`を終了し、記事保存と共通イベントの確定までを上流の責任にする。
- 救済から呼ばれる旧Taskiqタスク・workerは今回削除しない。共有Serviceの戻り値やエラー型を変更する場合、既存Taskiq呼び出し元への必要最小限の接続変更を行う。
- 旧救済経路に残るhold・日次上限の全廃、再投入方式、年齢による削除・対象外登録は別タスクで扱う。新経路にそれらを再実装せず、旧経路の撤去を本移行の完了条件にしない。
- 救済Taskiqと新Consumerが同じ記事へ到達することはあり得る。既存の保存済み確認と同一区分の一意制約による重複スキップを維持する。SignalとNoiseが同時に異なる表へ保存される場合の横断排他は、既存Assessmentと同様に本仕様では追加しない。

## Invariants

1. 発行元にかかわらず、記事完成イベントの種類・payload・意味が同じである。
2. 記事作成とそのイベント、Signal結果とAssessment向けイベントは、それぞれ同じトランザクションで確定する。
3. Noise・処理済み・Ready拒否・実行失敗から後続イベントを追加しない。
4. 新Consumerの失敗で記事を削除せず、stage hold・日次上限・独自再試行を導入しない。
5. Readyを作れないと確定した場合は、3工程とも理由を記録して受信完了とし、メッセージを削除して再配信しない。記事は削除せず、分析成功・処理済みとも区別する。
6. 新しい通常経路は旧2種類のイベントとTaskiq投入に依存しない。
7. 救済・旧イベント対応・過去のメトリクス整理を本工程の移行条件に含めない。

## Non-goals

- 救済のLambda／SQS／EventBridge移行、旧救済の検索・予算・期限切れ整理の再設計。
- 保存済みの旧イベントの移行・互換受信・再発行・削除。
- 旧Taskiq全体の撤去、AI全工程の旧hold関連コード・Valkey ACLの一括整理。
- 過去の未処理検索・管理画面・メトリクス・アラートの整理や互換維持。
- 本文取得・本文補完のイベント駆動化、Ready拒否の受信完了への統一を超えるAssessment／Embeddingの再設計。
- CurationのAI判定内容・モデル・プロンプトの変更、DB schema・認証認可・API responseの変更。
- Signal／Noiseをまたぐ新しい排他制御、汎用イベント基盤・汎用Consumer基盤の導入。

## 実装単位と検証

| 単位 | 実装する内容 | 主な確認条件 |
|---|---|---|
| 1. Ready拒否の契約と既存2工程の統一 | 理由付き受信完了、Assessment／EmbeddingのReady・Consumer・Lambda応答・関連仕様の変更 | 対象欠損・Ready入力制約違反が理由記録後に受信完了となること、AI・記事削除・後続イベントなし |
| 2. Curationの処理完了・失敗の契約 | Completion、共通ルールに従うReady拒否、原因を保持するServiceエラー、旧Taskiqとの接続 | Signal／Noise／処理済みとReady拒否の区別、保存失敗の原子性 |
| 3. Curation Consumer | DB事実取得、Ready構築、Service、Ready拒否の理由記録、失敗後処理 | AI前の接続返却、Ready拒否・処理済みのAI非実行、記事削除・hold・予算なし、元例外保持 |
| 4. Curation Lambda受信 | Geminiの借用、設定・資源管理、SQS・イベント検証、部分バッチ応答 | Ready拒否のmessageIdを失敗一覧へ含めないこと、資源終了、複数呼び出しでの独立性 |
| 5. 記事完成イベントと配送 | 共通イベント、上流2箇所の発行、Curation relay | 両発行元の同一契約、記事とOutboxの原子性、旧種別の非受信、保存済みID・時刻の維持 |
| 6. インフラ・通常経路の切替 | Consumer／relay／SQS／DLQ／Scheduler／IAM、上流kiqの終了 | 定義の接続、旧救済の存続、新経路からTaskiqを呼ばないこと |

スライス1はCurationだけの変更として済ませず、既存Assessment／Embeddingの受信完了まで変更・検証する。スライス3・4でCurationを同じ契約へ接続し、3工程すべての適合を今回のDoneとする。

### スライス1の確定契約（2026-09-13）

`AssessmentReadyBuildRejected`／`EmbeddingReadyBuildRejected`と各`ReadyBuildRejectionReason`は、それぞれの`domain/ready.py`に置く。`from_facts`と既存Taskiq向けの`try_advance_from`は、既存の`(Ready, 記事ID)`またはこの拒否値を返す。Consumer側に同名の型や変換用の例外を追加しない。

Consumerの戻り値は既存Completionまたは同じReady拒否値とし、ServiceのCompletionは変更しない。監査メソッドも`append_ready_build_rejected`へ統一する。欠損・処理済みの保存済み理由コード文字列は維持し、新規の入力不正も同系列の`assessment_ready_build_blocked_input_invalid`／`embedding_ready_build_blocked_input_invalid`を用いる。

Lambdaの完了ログは`reason=ready_build_rejected`と`rejection_code`を持つ。確定した拒否ではService・AI・成功監査・後続Outbox・成功／実行失敗メトリクスを実行しない。通常の監査障害でも受信完了を維持するが、キャンセル・プロセス終了は抑止しない。

拒否確定後の理由記録は業務処理の60秒制限を抜けた後に、各工程の`ConsumerFailureHandler.handle_ready_build_rejected`で行う。実行失敗用の分類・メトリクス・通知を経由せず、拒否監査とそのdrop処理だけを行う。

このスライスはAssessment／Embeddingの受信完了までを対象とし、Curation実装・救済移行・hold／日次上限の撤去・インフラ・AWS適用を含めない。共有Readyを使う旧Taskiq呼び出し元は値による分岐へ接続し、救済と資源ライフサイクル共通化を維持する。

実装・検証結果（2026-09-13）: スライス1は完了。Ruffのlint・formatが成功し、全単体6,655件と`make test-integration`の全DB統合1,402件が成功した。並行作業の拒否監査移動を取り込んだ最終状態で、関連単体745件・両工程のDB統合219件を再検証した。対象欠損・入力不正の拒否監査、DB由来IDと記事保持、同じ拒否値の伝播、監査・診断障害時の受信完了、業務タイマー解除後の監査、両Lambdaの混在バッチ応答を確認した。テスト用DB・Redisは終了処理で削除済み。CurationとAWSは未変更。

Ready拒否の検証は、各工程のConsumerで理由と副作用の不在を確認し、Lambda入口でReady拒否・処理済み・実行失敗の混在時に実行失敗のmessageIdだけが失敗一覧に載ることを確認する。既存の対象欠損を再配信する期待値は置き換える。SQSの削除動作そのものを模した重複テストは作らない。

未接続の部品実装と実際の切替を区別する。新Consumer・配送先が利用可能になる前に、稼働中の通常経路だけを停止しない。切替時に旧イベント対応を追加することは、本仕様では要求しない。

検証の責任は契約ごとに一箇所へ置き、既存のSignal保存・Outbox原子性などのテストを利用する。必要な追加は、共通イベントの両発行元、新Consumerの失敗境界、実handlerからの保存・資源解放、インフラ接続を中心にする。実装変更後は`/check`で該当範囲を検証し、実AWS検証を行っていない場合は明記する。

## Done

- 新しい記事が取得・本文補完のどちらで完成しても、同じ契約でCurationの通常経路へ進む。
- Curation・Assessment・Embeddingの全Consumerで、Readyを作れないと確定した場合に理由を記録して受信完了とする。LambdaはそのmessageIdを失敗一覧へ含めず、3工程すべての該当テストが成功する。
- 新Consumerの処理完了・Ready拒否による受信完了・AI／DB失敗・再配送を上記契約で説明できる。
- Signalは既存Assessmentのイベント経路へ接続され、Noise・処理済み・Ready拒否・失敗では後続を追加しない。
- relay・Consumer・SQS・DLQ・Scheduler・必要な権限の定義が接続され、通常経路のTaskiq直接投入を切り替えられる。
- 既存Taskiqの救済は存続でき、その移行や旧イベント整理を待たずに本工程を完了できる。

コード・定義の実装完了とAWSでの稼働切替完了は分けて報告する。実AWS上で確認していない状態を「移行済み」としない。本仕様書の作成段階では実装・デプロイを行わない。
