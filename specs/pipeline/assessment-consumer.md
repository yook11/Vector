# AssessmentConsumer — イベント受信と投資判定

Status: 正常終了・失敗理由の契約とConsumer・失敗後処理を実装・検証済み（2026-09-11）。Lambda・SQS・relayへの接続とAWS適用は未着手。

## Problem

Curationの完了イベントからAssessmentを実行し、対象内の判定結果をEmbeddingへつなぐ。イベント駆動に移したEmbeddingの責務分担を基準にする。既存AssessmentのRecoverable／Terminal、Taskiq retry、hold、backfillの方針は、新Consumerの設計根拠にしない。

## Evidence

- [Embedding Consumer仕様](./embedding-consumer.md)と[実装](../../backend/app/analysis/embedding/consumer.py)：正常終了、エラー伝播、失敗後処理の境界。
- [Embedding Lambda入口](../../backend/app/lambda_handlers/embedding/handler.py)：初期化、入力検証、部分バッチ応答、資源の終了。
- [Gemini通信設定](../../backend/app/ai_providers/gemini/settings.py)と[クライアント管理](../../backend/app/ai_providers/gemini/client.py)：DeepSeekの責務分担の参照元。
- [Curationイベント](../../backend/app/analysis/curation/events.py)、[Assessment保存処理](../../backend/app/analysis/assessment/service.py)、[Outbox送信契約](./outbox-sqs-message-contract.md)：既存のpayloadと保存・配送境界。
- [relay](../../backend/app/outbox/delivery/relay.py)と[Scheduler定義](../../infra/aws/outbox_relay.tf)：現在のコードはEmbedding向け配送と1分間隔の起動を定義している。Assessment向け配送は追加対象。AWSの稼働状態は本仕様では確認していない。

## 全体フローと責務

```text
CurationのSignal結果＋article.curated_signalをOutboxに保存
  → EventBridge Schedulerがrelay Lambdaを定期起動
  → relayがOutboxをポーリングし、article-assessment SQSへ送信
  → Lambda入口 → AssessmentConsumer → DeepSeek判定 → 結果を保存
      ├─ IN_SCOPE：article.assessed_in_scopeを同時にOutboxへ記録
      │    → relay → article-embedding SQS → EmbeddingConsumer
      └─ OUT_OF_SCOPE：後続イベントなし
```

- EventBridge Schedulerはrelayの起動を担い、Outboxの取得・配送はrelayが担う。新Consumerから後続のTaskiqタスクを投入しない。
- 入力は`article.curated_signal`、schema versionは1、payloadは`curation_id`と`analyzable_article_id`。イベント全体の形式は既存のOutbox送信契約に従う。
- Lambda入口は秘密情報・DB・AIクライアントの準備、SQSとイベントの検証、Consumer呼び出し、配送結果の応答、資源の終了を担う。
- Consumerは`curation_id`によるDB状態取得、Ready構築、Service実行、エラーの分類・後処理を担う。Taskiq ContextやSQSの形式には依存しない。
- Serviceは判定、保存時の状態確認、結果・成功監査・必要なOutboxのcommitを担う。
- DeepSeekは通信設定、クライアントの生成・終了、判定処理を分ける。Assessorは準備済みクライアントを借用する。秘密情報の取得と資源の利用範囲はLambda呼び出し単位とし、通信timeoutは接続3秒・読み取り10秒・書き込み10秒・プール待ち3秒、SDK・HTTP内部再試行は0回とする。

## 正常終了とエラー

| 結果 | 正常終了の根拠 | 後続イベント |
|---|---|---|
| `IN_SCOPE` | 対象内の判定結果・成功監査・Outboxをcommitできた | `article.assessed_in_scope`を記録 |
| `OUT_OF_SCOPE` | 対象外の判定結果・成功監査をcommitできた | なし |
| `ALREADY_ASSESSED` | 開始時または保存時に判定済みと確認できた | 追加しない |

エラーは正常終了の値に含めず、例外で伝える。Curation不存在、入力・AI応答の契約違反、provider障害、DB障害、timeout、想定外例外を含む。正常終了の結果は永続化・処理済み確認の結末であり、AIが返す判定内容とは区別する。

### 最初のタスク：正常終了の契約

Problem: 現在のServiceの`int / None`では、対象外の保存成功と処理済みによる終了を区別できない。イベント受信側が正常終了の根拠を判別できる契約を定義する。

- 既存の`AssessmentResult = InScope | OutOfScope`はAIの判定内容を表し、保存完了を保証しない。カテゴリー・投資見解・key pointsはこの判定内容に属する。
- Serviceから返す正常終了は`AssessmentCompletion`として、`kind`で`IN_SCOPE`・`OUT_OF_SCOPE`・`ALREADY_ASSESSED`の3種類を表す。Serviceの正常終了に`None`やエラー値を含めない。
- `IN_SCOPE`は今回の実行が対象内結果・成功監査・後続Outboxをcommitできた場合に返す。AIが対象内と判定しただけでは返さない。
- `OUT_OF_SCOPE`は今回の実行が対象外結果・成功監査をcommitできた場合に返す。失敗・不存在・処理断念の代替結果にはしない。
- `ALREADY_ASSESSED`はDB上で対象内または対象外の判定が確定済みと確認できた場合に返す。開始時に確認できた場合はAIを呼ばず、保存時に他の実行の完了を確認した場合は追加保存しない。どちらも成功監査・Outboxを追加しない。
- Repositoryは既存の契約を維持し、保存成功時は保存した行のID、`ON CONFLICT DO NOTHING`による重複スキップ時は`None`を返す。保存・DB障害は例外で伝える。
- ServiceはRepositoryの重複スキップを`ALREADY_ASSESSED`へ変換する。この結果名を返すための追加照会やロックは導入しない。対象内／対象外の同時保存を防ぐ排他制御は別タスクで扱う。
- 対象不存在や状態確認失敗は重複スキップと混同しない。一般的なUPDATEの更新件数0件を、そのままこのRepositoryの`None`と同じ意味には扱わない。
- commit失敗・結果未確定を正常終了に変換しない。元のイベントが再配信された際、DBで判定済みと確認できれば`ALREADY_ASSESSED`として完了できる。
- 後続イベントのpayloadは保存処理が確定したIDで構築する。Consumerの正常終了結果から再発行せず、配送はcommit済みOutboxからrelayが行う。

この段階の確認条件:

| 状況 | 期待する結末 |
|---|---|
| 対象内結果・成功監査・Outboxのcommit成功 | `IN_SCOPE` |
| 対象外結果・成功監査のcommit成功 | `OUT_OF_SCOPE`、Outboxなし |
| 保存時にRepositoryが重複スキップの`None`を返す | `ALREADY_ASSESSED`、追加の成功監査・Outboxなし |
| AI判定後に保存・監査・Outbox記録・commitが失敗 | 例外、正常終了なし |
| RepositoryがDB障害などの例外を送出 | 例外を伝播し、`ALREADY_ASSESSED`へ変換しない |

開始時の判定済みによる`ALREADY_ASSESSED`は、以下のConsumerスライスで同じ正常終了契約へ接続済み。

Non-goals: このタスクでは、エラーの詳細分類、排他制御の追加・変更、DeepSeek通信設定、Lambda・SQS実装、Taskiqの切替を行わない。既存のRecoverable／Terminalや後続タスク用IDの戻し方から正常終了契約を導かない。

Done: 3種類の正常終了の根拠、エラーとの境界、後続イベントの発行条件が上記の確認条件で説明でき、Service・既存呼び出し元への反映と単体・DB統合テストが完了する。

### 最初のタスクの実装状況

- `service.py`に`AssessmentCompletionKind(StrEnum)`と`AssessmentCompletion`（frozen・slots付きdataclass）を定義した。kindの値は`in_scope`・`out_of_scope`・`already_assessed`とする。
- 結果型は`kind`と`analyzed_article_id: int | None`を持ち、`IN_SCOPE`だけ正の整数IDを必須とし、他の結果へのID付与を拒否する。AIの判定内容を表す`AssessmentResult`は変更していない。
- Serviceは保存・commit成功またはRepositoryの重複スキップを正常終了へ対応付ける。RepositoryのID／`None`／例外の契約、SQL、ロック、DB schemaは変更していない。
- 既存Taskiqは`IN_SCOPE`だけ一覧更新通知、続いて結果型の記事IDによるEmbeddingタスク投入を行う。この接続は既存稼働の維持に限定し、新Consumerの後続配送は引き続きOutbox経由とする。
- 結果型の不正な組み合わせ、実DBでの重複保存、対象外と重複スキップの非通知・非投入をテストへ反映した。保存行・成功監査・Outboxの一致、重複時のcommit非実行、保存・commit・Outbox失敗時の例外とロールバックも検証した。
- 検証結果：`ruff check`と`ruff format --check`（app全体・変更したテスト）が成功。`uv run pytest tests/ -m unit -x -q`は6,366件成功。旧戻り値を期待していた統合テスト2件の更新後、該当ファイルの単体テスト14件を再確認した。`make test-integration PYTEST_ARGS='-x -q'`は1,354件成功・22件skipで終了し、テスト用DB・Redisは終了処理で削除した。今回の正常終了結果スライスは完了とする。

### 次のタスク：失敗理由の契約

Problem: AssessmentのServiceエラーを再試行分類から独立させ、Embeddingと同じく失敗理由と原因の詳細を呼び出し元へ伝える。

- `AssessmentFailureReason(StrEnum)`は`PROVIDER_ERROR=provider_error`、`RESPONSE_INVALID=response_invalid`、`CURATION_MISSING=curation_missing`を定義する。
- `AssessmentError`は`reason`・`provider_error`・`defect`を持つ。プロバイダー失敗だけ分類済みの`AIProviderStateError`または`AIProviderContentError`を必須とし、応答不正だけ`StrEnum`のdefectを必須とする。それ以外の詳細は`None`に限定し、不正な組み合わせは`TypeError`で拒否する。
- `code`はプロバイダーの`CODE`、応答不正の`defect.value`、不存在の`assessment_curation_missing`とする。`AssessmentResponseInvalidError(defect)`の呼び出し形式と全16種類の詳細コードを維持する。`AssessmentCurationMissingError()`も定義する。
- Serviceは`to_assessment_error`で元のプロバイダー例外を同一インスタンスとして保持し、`raise ... from exc`で原因チェーンをつなぐ。既存のAssessmentエラー・DB障害・timeout・想定外例外はそのまま伝播する。正常終了・保存内容・トランザクション境界は維持する。
- `SAFE_ATTRS=("code",)`とし、エラー文字列へ入力本文やSDK例外の自由文を追加しない。
- 既存Taskiqとの接続は`task_errors.py`の独立した`AssessmentTaskError`階層と`to_assessment_task_error`が担う。プロバイダー失敗は既存の分類属性へ対応付け、応答不正はRecoverable／`ai_response_invalid`、不存在はTerminal／`target_missing`へ変換する。Assessment以外の例外は同一インスタンスを返す。
- Taskiq入口は変換後の例外をspan・FailureHandlerへ渡し、変換時だけ元例外を原因として再送出する。既存Taskiqの再試行・hold・監査項目・メトリクスを維持する。旧mapper・旧importの互換aliasは残さず、Taskiq例外のモジュール名変更と原因チェーンへのAssessmentエラー追加は意図した変更とする。

Non-goals: 新Consumerや失敗後処理の実装、通信設定、配送切替、追加DB照会・ロック・schema変更は含めない。開始時のReady判定は変更せず、Curation不存在と開始時の判定済みを新Consumerへ接続するのは後続スライスとする。

Done: 原因保持・詳細コード・Taskiq動作の維持を単体・DB統合テストで検証し、Serviceのエラー契約から再試行分類を分離できること。

実装状況（2026-09-11）: エラー型・Service変換・Taskiq境界の接続を実装済み。プロバイダー全10種の原因保持、応答不正の全16コード、不正な構築の拒否、DB・timeout・想定外例外の同一性、既存Taskiqの監査・メトリクス・再試行・holdを検証した。実DBで詳細コードとTaskiq → Assessment → プロバイダーの3段の原因チェーンが監査へ保存されることも確認した。

検証結果: `ruff check`・`ruff format --check`（app全体と変更したテスト）が成功。`uv run pytest tests/ -m unit -x -q`は6,427件成功。`make test-integration PYTEST_ARGS='-x -q'`は1,369件成功・22件skipで完了し、一時DB・Redisは終了処理で削除した。失敗理由スライスは完了とし、Consumerへの接続は以下のスライスで実施した。

### Consumerと失敗後処理の実装

Problem: 正常終了・失敗理由の契約を、Taskiqに依存しないイベント受信側へ接続する。

- `AssessmentConsumer(session_factory, assessor)`は借用したAssessorを使い、`consume(ArticleCuratedSignal) -> AssessmentCompletion`を提供する。クライアントの生成・終了は担当しない。
- 業務処理の上限は60秒とする。既存の`curation_id`照会を1回だけ行い、取得したDB由来の記事IDを保持して読み取りセッションを閉じ、`ReadyForAssessment.from_facts`とServiceへ進む。イベントの記事IDは補完・照合に使わない。
- 開始時の対象内／対象外判定済みは`ALREADY_ASSESSED`を返し、AI・保存・成功監査・Outbox・成功メトリクスを追加しない。Curation不存在は原因付きの`AssessmentCurationMissingError`とする。Ready検証失敗でも取得済みのDB由来IDを失敗監査へ渡し、未取得なら`article_id=None`とする。
- `AssessmentFailureClassification`は監査用`FailureProjection`と任意の枯渇通知対象を持ち、`outcome`は持たない。プロバイダーのCODE・FAILURE_MODE・reasonを保持し、枯渇判定は共通関数を使う。応答不正は詳細codeと`ai_response_invalid`、不存在は`assessment_curation_missing`と`target_missing`、DBは共通DB投影、timeout・その他はunknown投影とする。
- 新AssessmentConsumerではDB・プロバイダー・その他の失敗を一律`processing_outcome{result=failed}`として計測し、`infra_error`区分を使わない。失敗率には原因を問わず処理失敗を含め、詳細は監査の型・コードで識別する。既存TaskiqやEmbeddingの計測契約は変更しない。
- 監査のretryabilityは観測情報だけに使い、失敗はすべて元例外のまま呼び出し元へ返す。ConsumerにTaskiq分類、hold、独自再試行は持ち込まない。
- 失敗後処理は60秒の外で、処理メトリクス・別セッションでの失敗監査commit・必要なプロバイダー枯渇通知を順番に独立して試みる。監査失敗時はaudit droppedを計測する。通常の二次例外や診断ログ障害で元例外を置き換えず、後続の処理を継続する。外部キャンセルは抑止しない。
- `AssessmentAuditRepository.append_classified_failure`はReadyを要求せず、分類を再計算せずに`FAILED`を記録する。既存の制限・マスキングを通したメッセージと原因チェーンを保存し、入力本文・AI生応答は収集しない。旧Taskiq用監査は維持する。

Non-goals: Lambda・SQS・relay接続、DeepSeek通信設定とクライアント管理、一覧更新通知、Taskiq停止、デプロイは含めない。SQL・保存契約・schema・ロックは変更しない。対象内／対象外の同時保存の排他制御と保存時不存在の専用エラー化は後続スライスとする。

Done: 正常終了、開始時判定、60秒制限、分類・監査・計測・通知、元例外の伝播が接続され、単体・実DB統合テストが成功すること。

実装状況（2026-09-11）: Consumer・失敗分類・後処理・監査入口を実装済み。既存の非同期Ready入口は`from_facts`へ委譲し、Taskiqの開始判定は維持した。正常保存と開始時／保存時の重複、IDの根拠、1回の照会とAI前の接続返却、全10種のプロバイダー分類と全16種類の応答不正コード、取得・AI・保存のtimeout、キャンセル、保存・成功監査・Outbox・commitの失敗とロールバック、後処理の二次障害を検証した。

検証結果: app全体と変更したテストの`ruff check`・`ruff format --check`が成功。監査drop記録箇所の追加に合わせた網羅性テスト更新後、`uv run pytest tests/ -m unit -x -q`は6,470件成功。統合テストの期待引数・DB制約と例外分類の前提・一時DDLの終了順序を修正後、`make test-integration PYTEST_ARGS='-q'`は1,422件成功・22件skipで完了し、一時DB・Redisも正常終了した。このConsumerスライスは完了とする。

## DeepSeek通信・クライアント管理スライス

Problem: Assessorが秘密情報の取得とクライアント生成を兼ねており、通信上限と資源の所有期間を呼び出し側から制御できない。

Evidence: Geminiの接続設定・クライアント管理、DeepSeekの既存判定spec、ワーカーと比較スクリプトの生成箇所を確認した。インストール済みopenai 2.44.0は`AsyncOpenAI.close()`が注入したHTTPクライアントを閉じる。timeoutと再試行指定は[SDK公式資料](https://github.com/openai/openai-python#retries)と[HTTPX公式資料](https://www.python-httpx.org/advanced/timeouts/)も参照した。

Invariants: 判定モデル・プロンプト・strict function calling・応答解析・プロバイダー例外変換・保存契約を維持する。Assessorは準備済みクライアントを借用し、生成・終了や秘密情報の取得を行わない。通常の終了障害と診断ログ障害で利用結果を上書きせず、外部キャンセルは伝播する。

- `DeepSeekConnectionSettings`はfrozen・slots付きdataclass。接続3秒・読み取り10秒・書き込み10秒・プール待ち3秒を既定値とし、正の有限数だけ受け付ける。秘密情報・モデル仕様は含めない。
- `open_deepseek_client(api_key, base_url, settings)`はasync context manager。既存の外部HTTPファクトリを使用し、SDK・HTTPの再試行を0回にする。SDKへ同じtimeoutを明示する。HTTPの読み取り上限はチャンク待ち時間であり、Consumerの業務処理全体60秒とは別の制限。
- SDK生成失敗でもHTTPを回収し、SDK終了後にHTTPが未解放なら回収を試みる。診断は資源名と例外クラスだけを記録する。空・空白だけのAPIキーは生成前に`AIProviderConfigurationError(NOT_CONFIGURED)`とする。
- `DeepSeekAssessor(client)`へ変更。`base_url`は既存の`DEEPSEEK_ASSESSMENT_SPEC`を配線側から渡し、定義を重複させない。
- 既存Taskiqはワーカー起動時に準備し、終了時・起動途中の失敗時に解放する。比較スクリプトは比較処理の間だけ所有する。SDKの遅延importを維持する。

Non-goals: Lambda・SQS・relay接続、Taskiq停止、他工程のDeepSeek利用箇所の変更、DB・schema・依存パッケージの変更は含めない。Lambda呼び出し単位の資源管理への接続は後続スライス。

Done: 借用・通信上限・内部再試行なし・生成失敗時の解放・終了障害時の結果保持を検証し、既存ワーカーを含む単体・統合テストが通ること。

実装状況（2026-09-11）: 設定・生成終了・借用・既存呼び出し元への接続を実装済み。app全体・変更テスト・比較スクリプトのruff lint・format確認が成功。全単体テストは6,518件成功、`make test-integration PYTEST_ARGS='-q'`は1,400件成功・22件skipで完了し、一時DB・Redisの終了も確認した。実APIは呼び出さず、通信はモックした。

## Invariants

- 対象外判定は正常終了であり、処理失敗によって対象外の判定結果を作らない。
- 新規の対象内保存時だけ後続イベントを記録する。配送成功をAssessmentの正常終了条件には含めない。
- 冪等性は`curation_id`単位で保証し、対象内と対象外が同時に確定しない。重複実行で結果・成功監査・Outboxを重複させない。AI呼び出しの一回性は保証しない。
- AI応答待ちにDBセッションや保存用ロックを保持しない。保存時の不存在はエラーとする。
- 処理エラーはすべてSQSへ失敗として返す。失敗分類は監査・計測・通知に使い、Consumer内の再配信判断には使わない。
- 再配信・上限到達後のDLQ移動はSQSに任せる。Consumer内で再配信待ちのsleep、独自backoff、hold、DLQへの直接送信を行わない。
- 業務処理の時間上限と失敗後処理の実行範囲を分ける。失敗後処理の通常例外で元のエラーを置き換えず、他の後処理を継続する。
- 初期化失敗はLambda呼び出し全体の失敗、識別可能な個別メッセージの失敗は部分バッチ応答とする。通常の終了処理・診断出力の失敗で確定済みの結果を変更しない。外部キャンセルやプロセス終了は抑止しない。
- 入力本文、秘密情報、SDK例外の自由文を診断へ無制限に出さない。

## 詳細化・有効化までに決めること

1. **実行・配送の数値**：業務処理の上限は60秒で確定済み。DeepSeek通信は接続3秒・読み取り10秒・書き込み10秒・プール待ち3秒、SDK・HTTP内部再試行0回で確定済み。残りはLambdaのtimeout、同時実行数、受信件数、SQS可視性timeout・保持期間・DLQ上限。Embeddingの値を参照し、Assessmentへの適用値を確定する。
2. **移行・再処理**：既存Taskiqとの併用期間と停止順序、既存backfillの扱い、蓄積済みOutboxの配信範囲、DLQの停止・調査・再投入手順。新Consumerのエラー契約とは分けて決める。
3. **一覧更新通知**：既存Taskiq入口にある保存後通知を新経路のどこで実行するか、通知失敗時の復旧をどうするか。[一覧の新着検知仕様](../news/article-list-update-notification.md)と整合させる。

エラー型・監査コード・監視分類とConsumer内のIDの扱いは実装済み。Lambdaでの入力検証・資源管理と保存時の排他方法は、上記方針に沿って後続スライスで詳細化する。

## Non-goals

判定カテゴリー・判定基準の変更、他工程のConsumer移行、旧Recoverable／Terminalを基準とする互換設計、全工程共通の抽象基盤の新設は含めない。

## Done

- Curationイベントの配送からAssessmentの保存、対象内の場合のEmbeddingへの配送まで接続できる。
- 3種類の正常終了、各エラーの失敗応答、処理中の削除、対象内／対象外を含む並行実行、commit失敗、timeout、後処理の二次障害を検証する。
- 対象内結果・成功監査・Outboxの原子性と重複防止を検証する。
- 有効化前に数値設定・移行・一覧更新通知の扱いを確定し、実装済みとAWS適用済みを分けて記録する。
