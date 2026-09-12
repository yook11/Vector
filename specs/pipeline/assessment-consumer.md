# AssessmentConsumer — イベント受信と投資判定

Status: Consumer・資源準備・共有イベント契約・本文解析に加え、Assessment Lambda入口とSQS部分バッチ応答を実装・検証済み（2026-09-12）。Assessment向けOutbox配送と別relay入口、Consumer・relayのTerraform定義を実装。AWSへの適用と稼働開始は未実施。

## Problem

Curationの完了イベントからAssessmentを実行し、対象内の判定結果をEmbeddingへつなぐ。イベント駆動に移したEmbeddingの責務分担を基準にする。既存AssessmentのRecoverable／Terminal、Taskiq retry、hold、backfillの方針は、新Consumerの設計根拠にしない。

## Evidence

- [Embedding Consumer仕様](./embedding-consumer.md)と[実装](../../backend/app/analysis/embedding/consumer.py)：正常終了、エラー伝播、失敗後処理の境界。
- [Embedding Lambda入口](../../backend/app/lambda_handlers/embedding/handler.py)：初期化、入力検証、部分バッチ応答、資源の終了。
- [Gemini通信設定](../../backend/app/ai_providers/gemini/settings.py)と[クライアント管理](../../backend/app/ai_providers/gemini/client.py)：DeepSeekの責務分担の参照元。
- [Curationイベント](../../backend/app/analysis/curation/events.py)、[Assessment保存処理](../../backend/app/analysis/assessment/service.py)、[Outbox送信契約](./outbox-sqs-message-contract.md)：既存のpayloadと保存・配送境界。
- [relay](../../backend/app/outbox/delivery/relay.py)と[Scheduler定義](../../infra/aws/outbox_relay.tf)：コード上の配送入口はEmbedding向けとAssessment向けに分離済み。既存Scheduler定義はEmbedding向けの1分間隔起動だけで、Assessment向けのAWS設定は未追加。AWSの稼働状態は本仕様では確認していない。

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

## Consumerの設定・資源準備・組み立てスライス

Problem: AssessmentConsumerとDeepSeekクライアントを、Lambda用の秘密情報・DB接続へ接続する。

Evidence: Embeddingの設定・資源管理・組み立てと、共通SSM取得・DB Engine生成・資源管理テストを参照した。

- `AssessmentConsumerSettings`はDB共通設定を継承し、`env`（既定production）、`aws_region`、`database_url`、`db_iam_auth`（既定true）、`deepseek_api_key_parameter_path`を扱う。dotenvは読み込まず、全環境でIAM必須、本番でTLS必須とする。接続情報の非表示を維持する。
- `open_assessment_resources(settings)`は利用範囲ごとにSSMからAPIキーを取得し、専用RDS署名器・Engine・session factoryを準備する。秘密情報はrepr対象外のSecretStrで保持する。
- `create_assessment_consumer_engine`は1接続・追加接続なし、プール待ち・接続・SQL実行の上限を各5秒にする。application_nameは`vector-assessment-consumer`。IAM署名器は必須引数とし、省略・Noneを拒否する。非IAM用の分岐を設けず、共通のTLS・pre-pingとDB例外変換を利用する。
- `app/lambda_handlers/assessment/composition.py`の`open_assessment_consumer(settings)`はasync context manager。資源、DeepSeekクライアント、Assessor、Consumerの順に準備し、借用Consumerを返す。公開handlerやSQSの入力形式には依存しない。
- 終了はDeepSeek、Engine、RDSの順。初期化失敗時も作成済み資源を解放し、同じ例外を返す。初期化の診断段階はresources・deepseek_client・consumerとし、yield後の業務例外は初期化失敗として記録しない。
- 初期化・終了の診断には段階／資源名と例外クラスだけを記録する。通常の終了・診断障害で結果を上書きせず、キャンセル・プロセス終了は抑止しない。準備・終了へ新しい時間制限は追加しない。

型の補足: 計画ではsession factoryを`async_sessionmaker`と記載したが、共通`caller_managed_session_factory`の実際の契約は`Callable[[], AbstractAsyncContextManager[AsyncSession]]`。DB例外変換を維持するため、AssessmentResourcesにもこの実際の型を使用した。

Non-goals: SQS・イベント検証、部分バッチ応答、Lambda公開handler、Terraform・IAM権限・Parameter Store作成、relay接続、Taskiq変更、デプロイは含めない。DB schema・依存パッケージも変更しない。

Done: 設定の読み取り範囲、Engine設定、資源の生成・終了順序と途中失敗時の解放、セッション終了時のロールバックを検証する。Consumerの保存・監査、共通DBのtimeout・再接続、共通SSM通信の詳細は既存テストへ任せ、このスライスでは重複追加しない。

実装状況（2026-09-11）: 設定・資源準備・Consumer組み立てを追加済み。テストは設定・Engine配線・資源の生成終了・実DBでのセッション終了へ絞った。IAM必須化後、app全体と変更テストのruff lint・format確認、全単体6,565件が成功。既存Embeddingテストの非IAM設定も署名器差し替えへ更新し、全統合1,402件成功・22件skip、一時DB・Redisの終了を確認した。SQSからの呼び出しは未接続。Embedding側も同じIAM必須契約へ統一し、実DBテストではAWS署名器のみをテスト用へ差し替える。

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


## SQS本文解析と共有イベント契約（2026-09-12）

Problem: 既存のArticleCuratedSignalはpayloadだけを定義していたため、SQS本文からイベント全体を安全に復元する入口を追加する。
Evidence: Curationの既存payload、EmbeddingのArticleAssessedInScopeEvent・本文解析、AssessmentConsumerのconsume契約を参照した。

### 共有契約

- `app/analysis/curation/events.py`の`ArticleCuratedSignalEvent`は、event_id・event_type・schema_version・occurred_at・payloadを持つfrozen・strict・extra禁止の型。既存ArticleCuratedSignalとConsumerの入力契約は変更しない。
- 種別は`article.curated_signal`、版は厳密な整数の1。payloadは既存型の正整数curation_id・analyzable_article_idだけとする。
- `from_input(data: object)`でUUID文字列・タイムゾーン付き日時文字列を復元する。JSON出力時の日時はUTCのZ表記とし、小数秒と保存済みID・payloadを保持する。
- 共有検証失敗は`CuratedEventValidationError`のfailureへ保持する。理由はinvalid_envelope・unsupported_event_type・unsupported_schema_version・invalid_payloadの順で優先し、詳細は失わない。payload自体の欠落・型不正は外側の構造不正、payload内部の違反はinvalid_payloadとする。
- `CuratedEventValidationIssue`と`CuratedEventValidationFailure`はfrozen・slots付き。項目は既知フィールドだけとし、未知キーはevent／payloadへ集約する。コードはmissing_required_field・invalid_type・invalid_value・unknown_field・unsupported_event_type・unsupported_schema_version。同じ項目・コードは重複排除する。
- エラーには入力値や検証ライブラリの自由文を収集せず、元のValidationErrorをcause・contextへ残さない。

### Assessmentの受信本文

- `app/lambda_handlers/assessment/event.py`の`parse_curated_signal_event(message_body: str) -> ArticleCuratedSignalEvent`はJSON解析後に共有契約へ委譲する。イベント内容を別実装で検証しない。
- 非文字列、壊れたJSON、重複キー、NaN・Infinityなどの非標準定数、解析時のRecursionErrorはinvalid_jsonとする。JSONとして正常な配列などは共有契約のinvalid_envelopeとなる。
- `AssessmentEventInvalidError`はCODE=assessment_event_invalid、reason、tupleのissuesを保持する。reasonは共有の4理由にinvalid_jsonを加えたAssessmentEventInvalidReason。SAFE_ATTRSはCODE・reason・issuesのみとし、共有の詳細値をそのまま引き継ぐ。
- エラー変換はexceptの外で送出し、本文や元の検証例外を原因チェーンへ残さない。想定外例外・プロセス終了を入力不正に変換しない。
- 戻り値はイベント全体。後続handlerがevent.payloadを既存Consumerへ渡す予定だが、このスライスでは接続しない。

### テストの責任と範囲

共有イベント契約の単体テストは、文字列／Python値からの正常復元、代表的な欠落・日時・種別・版・payloadの拒否、理由の優先順位、詳細の集約・安全性を11件で確認する。型や値の細かな組合せを網羅せず、重要な契約の代表例に絞る。JSON解析の単体テストは、実際の不正JSONと重複キー・非標準定数・深いネスト、正常な本文からの復元、代表的な共有違反の変換、想定外例外の同一性を担当する。詳細な項目不正の組合せは解析側へ重複して追加しない。

Non-goals: Records構造・messageId検証、Lambda handler、Consumer実行、部分バッチ応答、失敗監査・メトリクス、Assessment配送・relay接続は未実装。Embedding・DB schema・依存パッケージ・キュー設定・インフラは変更せず、local_testsの追加・実AWSスモーク・デプロイは行わない。
Done: 正常復元・安全な拒否・既存契約の維持と必要な回帰検証が成功すること。

検証結果: 追加単体テスト30件、app全体・追加テストのRuff lint／format確認が成功。`uv run pytest tests/ -m unit -x -q`は6,612件成功、`make test-integration PYTEST_ARGS="-x -q"`は1,400件成功（skipなし）。既存の非推奨・Logfire関連の警告は残る。DB・Redisの一時環境は終了済み。local_tests・実AWSスモーク・デプロイは今回の範囲外として実行していない。


## Lambda入口とSQS部分バッチ応答（2026-09-12）

Problem: 検証済み本文と準備済みConsumerを接続し、SQS受信から実行・応答までを進める。
Evidence: Embedding handler・FailureRecorder、共通SqsRecordBatch、Assessment compositionと既存の資源管理・入力検証テストを参照した。

### 公開入口と処理順序

- `app/lambda_handlers/assessment/handler.py`の同期`handler(lambda_event, context)`をパッケージからも公開する。返値は`{"batchItemFailures": [{"itemIdentifier": message_id}]}`形式のTypedDict。
- 共通ログ設定 → AssessmentConsumerSettings → asyncio.run → open_assessment_consumer → SqsRecordBatch検証 → 各本文の取得・解析 → consumer.consume(event.payload) → 利用範囲終了 → 応答の順に進む。
- Consumerと資源はバッチにつき1回準備し、レコードを入力順に逐次処理する。空バッチ・構造不正でも準備を先に行い、空バッチでは空の失敗一覧を返す。
- 各Consumer処理の既存60秒制限を維持する。入口に時間制限・再試行・Taskiq投入・通知を追加しない。

### 応答と診断

| 結末 | 入口の扱い |
|---|---|
| 設定失敗 | settings段階の初期化診断後、同じ例外を再送出 |
| compositionの初期化失敗 | 既存診断へ任せ、入口で二重記録せず伝播 |
| Records構造・messageIdの不正 | バッチ診断後に例外を再送出し、Consumerを呼ばない |
| 個別本文・イベントの不正 | そのmessageIdを失敗一覧へ追加し、次へ進む |
| 解析の想定外例外・Consumerの通常例外 | そのmessageIdを失敗一覧へ追加し、次へ進む |
| IN_SCOPE・OUT_OF_SCOPE・ALREADY_ASSESSED | すべて成功ログを記録し、失敗一覧へ含めない |

- 部分応答には入力順のmessageIdを加工せず載せる。初期化・構造不正では全件の失敗応答を合成しない。キャンセル・プロセス終了は個別失敗へ変換しない。
- `AssessmentLambdaFailureRecorder`がassessment_sqs_input_invalid、assessment_message_input_invalid、assessment_message_failedを記録する。構造不正はreason・field・record_index、個別入力不正はmessage_id・reason・安全なissues、処理失敗はmessage_id・error_classを持つ。本文取得失敗のreasonはinvalid_body。
- 検証済みイベントがある処理失敗にはevent_id・curation_id・analyzable_article_idを付ける。正常ログassessment_message_completedは同じ識別情報とreason=completion.kind.valueを持つ。
- イベント由来IDは配送診断に限定し、DB監査の主語へ補完しない。本文・例外の自由文をログへ出さず、通常のログ障害で結果・例外を変えない。
- 成功・失敗監査、メトリクス、Outboxは既存Consumer／Serviceへ任せる。クライアント・DBなどの終了も既存compositionの責任とし、入口から重複実行しない。

### 検証の責任と未接続部分

新規単体テストは7ケース。4件の正常な入力を成功・失敗・成功・失敗として処理し、失敗した2件のmessageIdだけを返すこと、同じConsumerへのpayloadの受け渡し、借用範囲終了後の応答、空バッチと代表的なID不正、設定とcompositionの失敗境界、安全な診断とログ障害、解析の想定外例外、処理中キャンセルを確認する。入力の細かな組合せ・初期化の全段階・個別資源の終了順序・DB保存と監査は既存テストへ任せる。

Non-goals: Assessment送信ルート、AWSイベントソース・ReportBatchItemFailuresの設定、IAM・デプロイ、Taskiq停止、一覧通知の移設は未実施。DB schema・依存・既存Consumerと資源管理の契約は変更しない。新規DBテスト・local_testsのシナリオは追加しない。
Done: 正常終了・個別失敗・バッチ失敗を適切に応答／伝播し、必要な接続テストと回帰検証が成功すること。

検証結果: 実装時点では新規8ケースを含む関連単体テスト72件が成功。app全体・追加テストのRuff lint／format確認、`uv run pytest tests/ -m unit -x -q`の6,620件、`make test-integration PYTEST_ARGS="-x -q"`の1,400件が成功した。既存の非推奨・Logfire関連の警告は残る。一時DB・Redisは終了済み。local_tests・実AWSスモーク・デプロイは今回の範囲外として実行していない。

PR作成前に混在バッチのテストを整理した。messageIdをテスト内に明示し、Consumerが4回処理されたことと失敗した2件だけの応答を確認する。修正後の対象テスト1件とRuff lint／format確認は成功。製品コードは変更せず、全体の成功済み検証は再実行していない。

## Assessment向けOutbox配送（2026-09-12）

- `app.lambda_handlers.outbox_relay.assessment_handler`を追加し、`article.curated_signal`だけをAssessmentキューへ送る。既存Embedding入口は維持し、2つの入口を別々のLambdaとして起動する設計とする。
- `AssessmentOutboxRelaySettings`は共通DB・region設定と`sqs_article_assessment_queue_url`を要求する。Embedding・Curation・CompletionキューのURLは不要。共通設定の既定値とTLS/IAM条件を維持する。
- 入口が種別・キュー・`build_curated_signal_message`を配送定義に結び付け、共通`run_relay(settings, route)`へ渡す。DB準備・1回のrelay実行・Engine終了を共有し、元例外と終了失敗の扱いは既存どおりとする。
- 本文生成は共有`ArticleCuratedSignalEvent`で検証し、保存済みイベントの全項目を維持する。送信エラーへの変換でも安全なreason・issuesを保持する。Consumer・受信handlerの契約は変更しない。
- 単体テストは本文の往復・代表的なエラー変換・用途別配線と設定を担当し、終了処理の既存テストは共通実行側へ移す。DB・通信・イベント詳細の保証は既存テストへ任せ、local_testsに重複シナリオを追加しない。

AWS上のAssessment relay Lambda、Scheduler、IAM、SQSイベントソースとReportBatchItemFailuresの設定は未接続。送受信のコードが揃った段階であり、定期配送を有効化した状態ではない。詳細は[Outbox送信契約](./outbox-sqs-message-contract.md)を参照。

検証結果: app全体と今回変更したテストのRuff lint・format確認が成功。`uv run pytest tests/ -m unit -x -q`は6,629件、続く`make test-integration PYTEST_ARGS="-x -q"`は1,400件が成功した。既存の非推奨・Logfire関連の警告は残る。一時DB・Redisは終了・削除済み。local_tests・実AWSスモーク・デプロイは対象外として未実施。

入口の配置・命名整理: `outbox_relay/handler.py`へ`embedding_handler`と`assessment_handler`を集約した。`__init__.py`は前者を`handler`として公開し、既存AWSの起動パスとインフラ設定を維持する。用途別パッケージは追加せず、共通実行・配送動作は変更していない。

配置・命名整理後の検証: app全体と変更テストのRuff lint・format、単体テスト6,629件、統合テスト1,400件が成功した。既存テストの参照先だけを更新し、ケースの追加は行っていない。一時DB・Redisは終了・削除済み。

設定と失敗変換の責務整理: Embedding専用設定を`EmbeddingOutboxRelaySettings`へ改名し、共通設定・Assessment専用設定と区別した。環境変数と既存AWS入口は変更していない。本文準備の失敗は`publishing.error_mapping.publish_preparation_error_from_exception`で扱い、分類済みPublishErrorの同一性、想定外例外のPREPARE_EVENTと原因チェーンを維持する。RoutedEventPublisherとSqsMessageBatchがこの処理を共有し、SQS固有のサイズ検証・SDK・資格情報・応答の分類はSQS側に残す。

PR作成時の最終検証: 設定名・本文準備エラーの責務整理と、既存のイベント／受信handlerテスト整理を含め、app全体・変更テストのRuff lint・format、単体テスト6,628件が成功した。統合テスト1,400件も同一の製品コードで成功し、一時DB・Redisは削除済み。統合検証後の追加対象は単体テストと文書のみ。テストガイドへ保証の所有先・1テスト1不変条件の方針を反映した。AWS設定・デプロイは未実施。


## migration適用済みDBでの実呼び出し検証（2026-09-12）

Problem: 従来の部品テスト用DB・Assessor差し替えだけでは確認できない、製品Engine・実SDK・handlerを含めた保存と接続管理を保証する。
Evidence: `local_tests/embedding/`、共通DB構築、AssessmentのConsumer・Service・Resourcesと既存テストを基準とする。
Invariants: migration適用済みDBへ製品Engineから`vector_app`で接続する。製品コード・DB schema・権限は変更しない。既存の設定・エラー・監査・メトリクスの契約を維持する。
Non-goals: AWS IAMの実認証、本番TLS、SQS実配送、AI実通信、デプロイ、対象内と対象外の同時保存に対する排他制御。
Done: 実呼び出しの確定保存・失敗時の原子性・同じ区分での重複抑止・資源解放を検証し、移した保証を既存テストに重複して残さない。

`backend/local_tests/assessment/`の12ケースで、実handler → 実DeepSeek SDK・Assessor → Consumer → Repository → 製品Engineを接続する。外部境界のSSM・IAM署名・HTTP応答を差し替え、DB操作は実物を使う。

| 所有するテスト | 保証 |
| --- | --- |
| `test_event_processing.py`（3件） | 指定記事の入力と対象内結果・成功監査・対応Outboxの確定、対象外結果・成功監査だけの確定、3種類の実INSERT後のSQL障害による全ロールバックと別トランザクションの失敗監査 |
| `test_duplicate_processing.py`（4件） | 再配送時の保存済み内容維持、同じ判定区分の同時保存で一意制約の実ロック待ちを経た後続の`ALREADY_ASSESSED`と先行内容の維持。対象内・対象外を各々検証 |
| `test_invocation_resources.py`（5件） | 連続呼び出しの1接続再利用と終了、AI待機中の接続返却・トランザクション終了、HTTP失敗・実DB障害・実DB待機中の業務期限切れ後の接続解放 |

保存結果はhandlerの応答後、別の`vector_app`接続から確認する。同時処理は異なるAI判定内容を返し、実INSERTの後に先行commitだけを停止して後続のDB待機を観測する。期限切れはDB待機を確認して既存の60秒timeoutをrescheduleする。製品のプール・通信設定をテスト用に緩和せず、テスト失敗時も待機を解除して呼び出し終了を待つ。

通常のConsumerテストから同時保存・接続返却の保証を移し、照会1回・DB由来ID・AIと成功メトリクスの非実行は残す。Serviceの重複テストは`ALREADY_ASSESSED`とcommit非実行に絞り、Resourcesの旧DBテスト2件は実呼び出しの接続管理へ置き換える。部品ごとの保存・成功監査・Outbox・commit失敗の伝播、元例外保持、設定、資源の生成終了順と二次障害は通常の`tests/`に残す。

AWS上の別relay Lambda・定期実行・Consumerのイベントソース接続は引き続き未実施。

検証結果: app全体・変更した通常テスト・追加したlocal_testsのRuff lint／format確認が成功。Assessment単独12件と、`make test-local`による共通DB・Embeddingを含む全65件が成功した。`uv run pytest tests/ -m unit -x -q`は6,634件、続く`make test-integration PYTEST_ARGS="-x -q"`は1,396件が成功（skipなし）。既存の非推奨・Logfire関連の警告は残る。一時DB・Redisは終了・削除済み。別作業中のEmbedding変更は編集対象に含めていない。


## AssessmentのTerraform定義と検証

Problem: 実装済みAssessmentの受信入口とOutbox配送をAWS資源へ接続できる定義を用意する。
Evidence: Embedding Consumer、既存Outbox relay、bootstrapのIAM境界、CIのdigest保持とTerraformテストを基準とする。
Invariants: 既存Embeddingの資源アドレスと入口を維持する。Assessmentは専用Consumer・専用relayとし、配送先・秘密情報・通信先を限定する。通常plan/applyでは各イメージdigestを保持する。
Non-goals: AWSへのapply、秘密値の登録、実AWSスモーク、Taskiq停止、DB schema・権限の変更。
Done: Consumer・relay・SQS/DLQ・IAM・ネットワーク・CIの定義が接続され、fmt・validate・モックproviderのplanテストと関連スクリプトの検証が通ること。

設定はEmbeddingを基準とする。Consumerはarm64・1,024MB・120秒・予約同時実行10、SQS受信は1件・待機窓0秒・最大同時実行10・ReportBatchItemFailuresを使う。業務上限60秒は変更しない。元キューは保持4日・可視性720秒・5回で専用DLQへ移動し、DLQは14日保持・既存SNSへの滞留通知を使う。専用relayは512MB・120秒・予約同時実行1・1分間隔とする。

DeepSeekキーの参照先は`/<prefix>/assessment-consumer/deepseek-api-key`とし、Terraformは秘密値を作成・保持しない。Consumer用private subnetは既存と重複しないindex 29を使い、RDS・SSM・DeepSeek専用proxy経路へ接続する。relayは既存relayのDB・SQS向けネットワークを共有し、専用実行ロールとSchedulerロールを持つ。

Consumerとrelayのdigestはそれぞれ独立させ、未指定かつ既存資源なしならLambdaと起動トリガーを作らない。digest指定時の有効化は既存Embedding方式に揃える。適用前にbootstrapの権限境界更新、秘密情報の登録、イメージ準備、既存キュー滞留とTaskiqとの稼働切替を確認する。


Terraformの公開設定・接続先:

| 項目 | 定義 |
| --- | --- |
| Consumerのイメージ | `assessment_consumer_image_digest` |
| relayのイメージ | `assessment_outbox_relay_image_digest` |
| Consumer入口 | `app.lambda_handlers.assessment.handler.handler` |
| relay入口 | `app.lambda_handlers.outbox_relay.assessment_handler` |
| キュー | 既存`aws_sqs_queue.outbox["assessment"]`を更新し、同名キューを再作成しない |
| 秘密情報 | output `assessment_consumer_parameter_path`のSecureStringを別途登録する |

plan/applyのCIは`resolve-assessment-images.py`で2つのdigestを独立して保持する。明示指定したdigestはbackend ECRに存在することを確認してからplanへ進む。state取得・解決・ECR確認が失敗した場合は処理を停止する。既存Embeddingのdigest・Lambda入口・資源アドレスは維持した。

bootstrapには専用実行ロール・Schedulerロールの権限境界、CIの管理許可・PassRole制約・Lambda設定の限定復号対象を追加した。CI inline policyの容量を超えるため、Outbox relayとAssessmentのboundary固定Denyを`apply_outbox` managed policyへ移した。移設先を適用してからinlineを更新し、全ロールの元のDenyが1件ずつ残ることをモックplanで照合する。容量は実際の形式・長さのARNで検証する。

検証の責任は次のとおりとする。

- Terraform本体: Assessmentの入口・キュー/DLQ・通信経路・権限・未起動条件を6件で確認し、既存Embeddingの7件も維持する。
- bootstrap: Assessmentの権限境界・CI管理範囲と容量・Deny移設の完全性を3件で確認し、既存6件も維持する。
- スクリプトとCI: 2つのdigestの独立保持・片側更新・不正stateによる出力停止、実CI shellのstate失敗と両イメージのECR存在確認を11件で確認する。
- 実AWSの認証・IAM評価・配送・proxy到達・DLQ移動は今回実行していない。アプリの保存・DB接続管理は既存local_testsに任せ、Terraformテストへ複製しない。

Terraform検証はtfvars・state・ローカルbackend設定を除いた一時コピーで、`init -backend=false -input=false -lockfile=readonly`、`validate`、`test`を実行する。テストは[公式のmock provider](https://developer.hashicorp.com/terraform/language/tests/mocking)を使うplanだけとし、AWS資源は作成しない。SQS可視性720秒・Lambda上限120秒は[公式のSQS連携設定](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)の推奨関係を満たす既存Embedding値を引き継いだ。

検証結果（2026-09-12）: 変更Terraformの`fmt -check`、本体とbootstrapの`validate`、mock providerによる本体13件・bootstrap9件のplanテストが成功した。既存ロックファイルのAWS provider（本体6.62.0・bootstrap6.56.0）をローカルキャッシュから利用した。Ruff lint／format（app全体・追加スクリプト・追加テスト）が成功し、追加スクリプトテスト11件を含む全単体6,645件、続くDB統合1,396件が成功した。DB・Redisの一時環境は削除済み。actionlintは変更前からある`concurrency.queue: max`への未対応診断1件を確認し、その診断だけを除外した検査は成功した。既存のTerraform非推奨・Logfire関連の警告は残る。

AWS向けplan/apply・実AWSスモーク・秘密値登録は実行していない。TerraformテストはAWS接続を伴わない一時コピーで実行し、既存のlocal_testsの再実行はアプリ変更がないため行っていない。
