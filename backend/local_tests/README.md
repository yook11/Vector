# migration適用済みDBを使うローカルテスト

実行: リポジトリルートで`make test-local`。

`test_source_acquisition_requests.py`は、Scheduler入力から実DBの有効なソースを選定し、取得依頼一覧を作る経路を担当する。DB変更後の再選定と、cadence・登録状況による除外を確認する。対象外の理由は既存の確認結果に残り、取得依頼一覧には混ぜない。ID生成の時差同一性・入力拒否は通常の単体テストが担当し、同じ選定シナリオを重複させない。SQS送信まで接続し、成功済みを除く再送・上限到達・再送対象外の失敗を検証する。SQSのSDK呼び出し境界と待機だけを差し替え、対象選定・依頼生成・送信アダプター・再送判断は実物を使う。Lambda入口の実装時にはこの経路を拡張する。

Docker、backendの`uv sync --frozen`、frontendの`npm ci`、Node/npmが必要。
Better Auth CLIは既存のfrontend developmentイメージと同じ版をnpxで起動する。

## 配置と保証範囲

ここには、ローカルに用意した実DB環境でしか確認できないテストを置く。
DB不要の接続URL・準備処理の単体テストは通常の`tests/`に置く。

検証場所の集約とケースの統合は分けて考える。条件や期待結果が異なる場合は別ケースとし、操作と検証が同じならparametrizeを使える。準備・接続設定・外部応答の形式はfixtureやsupportへ分離し、ケース固有の入力と重要な期待値はテスト本文に残す。混在バッチや競合など、複数対象の相互作用を保証する場合は一つのケースで扱う。

テストファイル追加時は既存のbasenameと`__init__.py`の有無を確認し、packageでない工程ディレクトリ間では固有の工程名をファイル名に含める。限定実行の前に`cd backend && uv run pytest local_tests/ --collect-only -q`で全体の収集を確認する。

```text
local_tests/
├── database.py                   共通DBの構築・分離
├── conftest.py                   共通fixture
├── http.py                       共通のHTTP応答差し替え・リクエスト記録
├── test_article_analysis_lifecycle.py  共通ライフサイクルと実DB資源管理（11件）
├── test_database_permissions.py  3ロールの許可一覧・実操作（40件）
├── test_auth_provisioning_schema.py  既存のAuthデータ契約（2件）
├── test_database_isolation.py    実DBのケース分離・失敗時の回収（2件）
├── acquisition/test_article_acquisition.py  取得記事の充足・不足による保存先とイベント（2件）
├── embedding/conftest.py         共通の接続設定・HTTP境界fixture
├── embedding/support.py          記事準備・実ハンドラー呼び出し
├── embedding/test_session_boundaries.py  AI待機前のセッション返却（1件）
├── embedding/test_event_processing.py  対象記事の保存成功・保存失敗・通信失敗・DB待機期限切れ（4件）
├── embedding/test_duplicate_processing.py  重複配送・同時処理での保存結果の維持（2件）
├── assessment/conftest.py        接続設定・HTTP境界・実DB障害と待機の制御
├── assessment/support.py         記事準備・実ハンドラー呼び出し・別接続からの確認
├── assessment/test_event_processing.py  対象内・対象外の確定保存・原子性・通信失敗・DB待機期限切れ（5件）
├── assessment/test_duplicate_processing.py  再配送・同じ判定区分の同時保存（4件）
├── assessment/test_session_boundaries.py  AI待機前のセッション返却（1件）
├── completion/conftest.py        補完用設定・接続・制御のfixture
├── completion/http_control.py   記事HTTP応答の停止・再開
├── completion/commit_control.py 補完の確定直前の停止・実SQL障害
├── completion/support.py        記事準備・別接続からの確定結果の確認
├── completion/test_event_processing.py  正常終了・完成保存・失敗判断・原子性（12件）
├── completion/test_duplicate_processing.py  再配送・並行競合（5件）
└── completion/test_session_boundaries.py  HTTP待機中・キャンセル時の資源解放（2件）
```

- `database.py`: Alembic headへの到達、必要なAuthテーブルとカテゴリ初期データの存在を準備時に確認し、不成立ならテストを開始しない。DB構造全体や既存データの移行を網羅するテストではない。
- `test_database_permissions.py`: 3ロールのテーブル・列・採番の実効権限が許可一覧に一致することと、管理権限の不在、代表的な保存・CRUD・禁止操作を確認する。詳細は[ロール権限仕様](../../specs/pipeline/database-role-permissions.md)を参照する。
- `test_auth_provisioning_schema.py`: ユーザー作成の実DB動作テストへ置き換えるまで、既存の列・一意制約の契約2件を保持する。
- `test_database_isolation.py`: 実DBが必要な基盤の検証として、commit済みデータ・採番の分離と、失敗時の未解放接続の回収を確認する。

`embedding/test_event_processing.py`は、内容の異なるイベントで指定していない記事を先に作り、対象記事のイベントだけを実ハンドラーに渡す。
HTTP境界で実SDKの送信本文を記録し、対象記事の本文を含みイベントで指定していない記事の本文を含まないことを確認する。
テスト側で独立して作る全要素が異なる768次元のベクトルを返し、応答後の別接続で対象記事への確定保存とイベントで指定していない記事の未更新を確認する。
入力整形・モデル設定の網羅や接続解放はこのケースへ追加せず、既存の部品・リソース管理テストに任せる。
詳細は[イベント処理仕様](../../specs/pipeline/embedding-event-processing.md)を参照する。

`embedding/test_duplicate_processing.py`は、同じSQSレコードを実ハンドラーへ2回渡し、両方の正常応答と保存済みベクトルの維持を確認する。
初回の保存は別接続で確認し、再配送時には異なるAI応答を用意して、誤った上書きを検出する。
生成済み判定やAI・監査を呼ばないことの部品テストは残し、このケースでは再配送後の確定済みデータを確認する。
同時処理のケースは両方をAI生成まで進め、異なるベクトルを返す。
先行側の実UPDATE後にコミットを一時停止し、後続側がその接続のロック解除を待つことを`pg_blocking_pids`で確認してから先行側を再開する。
実Consumerの結果が保存成功1件・生成済みによるスキップ1件であり、両ハンドラーが正常終了し、別接続から先行側のベクトルを確認できることを検証する。
スキップするのは保存処理で、両方ともAI生成は行う；待機は期限を設け、確認途中で失敗しても一時停止を解除して呼び出しの終了を待つ。

`test_article_analysis_lifecycle.py`は`open_article_analysis_consumer`を直接呼び、記事単位AI分析に共通する資源管理を一箇所で確認する。工程別handler・composition・SDKは使用せず、工程名によるparametrizeもしない。
AIには開閉を観測するテスト用context managerを渡し、SSM取得とRDS署名を外部境界で差し替える。DBは製品のEngine生成処理・IAM password provider・session factoryを通して、migration適用済み環境へ接続する。
準備順序、借用中の生存期間、AI→Engine→RDSの解放順序、呼び出し内の接続再利用と呼び出し間の資源分離を確認する。
例外・キャンセル・初期化失敗・実SQL障害・プール飽和・コマンド期限切れ・切断後の回復でも実接続を使い、dispose完了と使用した接続IDの消滅を別接続から確認する。切断反映の待機上限は2秒とする。
通常の部品テストから移した、借用セッション間のロールバックもここで確認する。SDK内部の終了方法は各SDKの部品テストが担当する。

`embedding/test_session_boundaries.py`と`assessment/test_session_boundaries.py`は各Consumerの読み取りセッション境界を確認する。
HTTP応答を到達合図で停止し、貸出接続0件、実DB接続がidleかつxact_startなしであることを確認する。共通ライフサイクルは記事処理内のセッション境界を決めないため、この保証は各工程に残す。
確認途中で失敗してもfinallyでHTTP応答を再開し、呼び出しの終了を待つ。

`embedding/test_event_processing.py`の通信失敗ケースは未保存・失敗応答と次の記事の保存を確認する。
DB待機期限切れのケースは対象行をロックし、pg_blocking_pidsで実際の待機を確認してからConsumerの実timeoutをrescheduleする。未保存・失敗応答を確認し、ロックと期限操作の差し替えを解除して同じ記事を正常保存する。
資源解放のassertを工程ごとに繰り返さず、保存と応答を検証する。

監査の詳細項目・エラー分類・混在バッチの部分失敗応答・SDK終了順は通常の部品テストで確認する。

Assessmentも実handler → 実DeepSeek SDK・Assessor → Consumer・Repository → 製品Engineを通す。
DBは共通のmigration適用済み環境へ`vector_app`で接続し、IAM署名・SSM取得・AI HTTP応答だけを外部境界で差し替える。
本番と同じ1接続・追加接続なし・各DB timeout 5秒を使う。AWS IAMの実認証・本番TLS・AIへの実通信は確認しない。

- `assessment/test_event_processing.py`: 対象記事の入力・対象内結果と成功監査・対応Outbox、対象外結果と成功監査だけの保存を確認する。障害ケースでは同一トランザクション内に結果・成功監査・Outboxが実INSERT済みであることを観測し、実SQLエラーを起こす。応答後の別接続から全件のロールバックと別トランザクションの失敗監査を確認する。
- `assessment/test_duplicate_processing.py`: 再配送で異なるAI応答を用意しても保存済み内容が変わらないことを確認する。同時処理では両方をAIまで進め、先行側の実INSERT後に確定を停止する。後続のINSERTが一意制約のロック待ちになったことをDBで観測してから再開し、先行側の内容だけが残り、後続が`ALREADY_ASSESSED`になることを確認する。対象内同士・対象外同士の2種類を扱い、対象内と対象外が競合するケースは保証しない。
- `assessment/test_session_boundaries.py`: AI応答待ちでの接続返却とトランザクション終了を確認する。
- `assessment/test_event_processing.py`の失敗ケース: HTTP失敗時の失敗応答・監査と次の記事の正常応答、実INSERT待機での業務期限切れ後のロールバックと同じ記事の再処理を確認する。期限切れは実ロック待ちを観測してから既存timeoutをrescheduleし、設定の60秒を待たない。

Assessmentの既存Consumerテストから同時保存と接続返却の保証を移し、共通資源の実DBテストは`test_article_analysis_lifecycle.py`へ集約した。
通常のConsumerテストには照会回数・DB由来の監査ID・判定済み時のAIとメトリクスの非実行を残し、Serviceの重複テストは`ALREADY_ASSESSED`とcommit非実行に絞る。
成功監査・Outbox・commit各境界の失敗伝播、元例外保持、工程別設定・配線、終了処理自体の二次障害は、引き続き通常の`tests/`が担当する。
製品コードをテスト用に変更せず、実Engineと処理は保持したまま観測・DB障害・競合の順序だけを制御する。
詳細は[Assessment仕様](../../specs/pipeline/assessment-consumer.md)を参照する。

DB不要の単体テストは`../tests/test_local_database.py`（接続URL・準備処理の後片付け、5件）と
`../tests/test_iam_fixtures.py`（共通IAM署名差し替え、11件）にある。
`tests/analysis/embedding/`に残る業務判断・エラー分岐のテストは従来のDB環境を使う。
以前の`make test-system`は`make test-local`へ改名した。

目的別に実行する場合はbackendで次を使う。

```sh
uv run pytest local_tests/test_article_analysis_lifecycle.py -x -q
uv run pytest local_tests/embedding/ -x -q
uv run pytest local_tests/assessment/ -x -q
uv run pytest local_tests/test_database_permissions.py -x -q
uv run pytest local_tests/test_database_isolation.py -x -q
uv run pytest tests/test_local_database.py tests/test_iam_fixtures.py -x -q
```

## 共通fixture

- `system_database_template`: 一式で1回、専用Postgres・既存ロール初期化・Better Auth・Alembic headを構築する。
- `system_database`: 各ケースへ適用済みDBのコピーを渡し、終了後に削除する。
- `system_database.connect("vector_app")`: 実アプリ権限で接続する。
- `system_database.url("vector_app", sqlalchemy=True)`: 製品Engineへ渡すURLを得る。
- `system_database.connect("vector")`: データ準備用の管理接続であり、権限試験には使わない。

```python
async def test_article_read(system_database):
    async with system_database.connect("vector_app") as connection:
        assert await connection.fetchval("SELECT current_user") == "vector_app"
```

工程別システムテストは`<工程名>/`配下に配置し、共通fixtureを利用する。
既存の`tests/conftest.py`は読み込まず、`create_all`や全テーブルTRUNCATEを使わない。
clone元には接続しない。各workerは独立したComposeプロジェクトを持つ。
ロールはクラスタ内で共有するため、各ケースではロール定義を変更しない。
DBレベルの独自ACLはcloneされないため、検出時は初期化を失敗させる。
通常終了・テスト失敗・初期化失敗時に専用コンテナとネットワークを削除する。

`.env`は使わず、DB接続値は専用Composeの定義から取得する。
DBロールとテーブル権限は既存の初期化／migrationを正本とし、テストを通すためのGRANTを追加しない。

ローカルのDB契約・Embedding／Assessmentシステムテストであり、AWS IAMの実認証・SQSの実配信・AIへの実通信・同一アプリイメージでの実行は保証しない。
詳細は[共通DB仕様](../../specs/pipeline/system-test-database.md)を参照する。

既存の`tests/test_db_user_isolation.py`の権限20件は許可一覧の照合と実操作に再編した。独自の接続先解決と環境不足によるskipは廃止し、Authの構造契約2件も共通DBで実行する。Outbox個別migrationのupgrade/downgrade試験は`tests/outbox/test_collect_permissions_migration.py`に残す。

記事単位AI分析の資源管理は`app.lambda_handlers.article_analysis_lifecycle`を通す。共通テストはこの入口を直接検証する。工程別のセッション境界テストでは、共通`analysis_engines` fixtureで借用先Engineを観測し、実SDK・Consumer・DBを通す。


## 取得記事の保存とイベント記録

`acquisition/test_article_acquisition.py`は、実RSS取得・変換・DB保存を通して次の2ケースを確認する。

- 本文が揃った記事は完成記事として内容を保存し、対応する`article.analyzable_created`だけを記録する。
- 本文不足の記事は取得できた情報を未完成記事として保存し、対応する`article.incomplete_recorded`だけを記録する。

各ケースは記事1件を扱い、反対側の保存先が空であることも確認する。RSSのHTTP応答だけを差し替え、取得Service・変換・保存・イベント生成は実物を使う。取得は`vector_collect`、結果の読み取りは別接続の`vector_app`で行う。

従来の`curation/test_delivery.py`をこの取得工程のテストへ置き換えた。Completionの実行、Relayの配送、Gemini応答、Curation結果の検証は含めない。補完の保存・完成イベントは`completion/`、Relayの配送契約は`tests/lambda_handlers/test_curation_relay_integration.py`、Curationの処理は既存のCurationテストが担当する。従来の取得・補完からRelayを経てCurationへ至る一連の接続保証は、今回の2ケースでは行わない。

## CompletionのConsumer接続（テストファースト）

`completion/`はConsumer → HTTP取得 → HTML抽出 → 記事構築 → 実DBの確定を対象にする。実装済みの`app.collection.article_completion.consumer`を接続し、実際の抽出・構築・DB確定まで検証する。`test_completion_delivery.py`ではSQSイベントを実Lambda handlerへ渡し、同じ確定結果から部分バッチ応答までを確認する。

補完側の`conftest.py`は、テスト専用設定・Collect権限のDB接続・実Consumer・HTTP応答と待機・確定制御のfixtureを組み立てる。DBの作成・分離・破棄は既存の`database.py`と共通fixtureへ任せる。HTTPの差し替えと記録は共通の`local_tests/http.py`を使い、robotsの許可や記事の応答内容は補完側で決める。

fixtureは環境・コンポーネントの準備と寿命の管理に使い、既存関数にDBを渡すだけのfixtureは作らない。`support.py`の記事データ準備・削除・確定結果と接続状況の読み取りは、普通のヘルパーとして本文から呼ぶ。応答待機は`completion/http_control.py`とfixtureで準備・解除し、確定直前の停止・実SQL障害は`control_commit` fixtureで利用できるようにし、`completion/commit_control.py`のcontext managerで開始・解除する。競合の再現手順は`completion/concurrent_processing.py`へ置き、`run_completion_race` fixtureがDB・2つのConsumer・待機制御を組み合わせて渡す。

イベント処理は「補完不要」「完成の一括確定」「失敗判断とDB状態」の3クラスに分け、行なし／closedと失敗監査障害時の再試行／終了は個別のテストにする。セッション境界ではHTTP待機中の観測タイミングとassertを本文に残す。重複・競合を含む先行17ケースの条件と期待結果を維持し、初回DB照会障害と外部キャンセルの2ケースを追加する。全19ケースが実Consumerの`consume()`を呼び、追加処理不要の理由も確認する。

公開入口は`ArticleCompletionConsumer(session_factory).consume(incomplete_article_id)`とする。戻り値は`CompletionSucceeded`（完成記事IDを保持）、`CompletionNotRequired`（missing・closed・superseded・url_conflictの理由付き）、`CompletionFailed`（元例外を`error`、RetryArticleCompletion / CloseArticleCompletionを`decision`に保持）の3種類として検証する。Consumerは通常の補完失敗を値で返し、配送handlerが再試行判断だけを失敗一覧へ対応付ける。型の配置や内部の分解を固定せず、これらの名前をConsumerモジュールから参照できることを入口の契約にする。

| テスト | 振る舞い |
|---|---|
| test_event_processing.py | 対象なし・closedでHTTPなしの正常終了、open / runningで対象記事の原子的な完成、429の元例外と待機判断、403のclosed確定、別経路の同URL完成済み記事の維持 |
| test_event_processing.pyの障害ケース | 完成保存の4操作後のロールバックと再処理、closed確定失敗の再試行、失敗監査の障害が元の判断を変えないこと、初回DB障害を対象なしと見なさないこと |
| test_duplicate_processing.py | 完成後の再受信、成功／closedの4通りの並行競合で先行確定を維持すること |
| test_session_boundaries.py | HTTP応答待ちでのDB解放と、外部キャンセル時のHTTP・DB資源解放 |
| test_completion_delivery.py | 混在バッチの保存結果と部分応答、再試行後の完成、closed確定失敗の再配信、完成後の再受信、DBのソースを正とする処理 |

ConsumerのテストはHTTPをMockTransportで差し替え、抽出・構築・Repositoryは差し替えない。DBは共通system_databaseを使い、書き込みはvector_collect、確定後の内容確認はvector_appの別接続で行う。未確定の保存は書き込み接続の許可列（監査id・Outbox event_id）の件数と対象記事の状態で確認する。実SQLエラーはflush後・commit前に発生させる。並行処理はHTTP応答とcommitを制御し、pg_blocking_pidsで実際の競合待機を確認してから先行処理を再開する。SQLの書き方やロック方式自体は固定しない。

配送テストは実handler・設定型・資源管理・Consumer・製品Engineを通し、IAM認証を有効にしてvector_collectへ接続する。署名結果だけを共通`inject_test_db_signer`でローカルDBの認証情報へ置き換え、SQSクライアントとHTTPを外部境界で差し替える。混在バッチでは失敗後の記事も完成することと、本文不正の記事が未変更であることを確認する。既存Consumerテストの分類表や競合・資源管理の保証を配送テストへ複製しない。

HTTP分類・文字コード・構築条件・監査の全属性は部品テストに任せ、同じ条件表を繰り返さない。旧経路の独立した保証は残す。SQS・Lambda・IAM・通知の実通信は今回保証しない。

対象だけを実行する場合は`cd backend && uv run pytest local_tests/completion -q`、全体確認は`make test-local`で行う。Consumerの読み込みはfixtureがテスト専用設定を用意した後に行う。

補完の新経路は抽出時の重複除去を無効にしているため、補完fixtureで抽出器のキャッシュをリセットしない。同じHTMLの再処理・別記事の先行処理でも素材が欠落しない保証は、`tests/collection/article_completion/test_html_extraction.py`で実抽出器を使って確認する。

## Backfillの入口と共通資源管理

`backfill/test_backfill_delivery.py`は3工程の実Lambda入口を個別に呼び、保存済みID・時刻が各キューへ配送されることを確認する。未完了記事の再投入は独立した1ケースに置き、配送ケースに資源管理のassertを混ぜない。

`backfill/test_backfill_lifecycle.py`は共通の`open_backfill_resources`を直接呼ぶ。工程名でparametrizeせず、実接続の生存期間・再利用・呼び出し間の分離、SQL障害・外部キャンセル・初期化失敗・終了障害を具体名のケースで確認する。接続を使用したケースでは、製品のdispose完了後に別接続の`pg_stat_activity`から接続IDが消えたことを確認する。初期化途中でまだDB接続がないケースは、取得済みengine・RDSの解放だけを確認する。

DBは共通`system_database`のmigration適用済み環境を使い、実行は`vector_app`ロール、データ準備は管理用接続とする。IAM署名とSQS通信だけを外部境界で差し替える。テーブル作成・DB権限の一覧確認を独自に追加しない。

通常の`tests/lambda_handlers/backfill/`には設定、時刻固定、無効時の起動抑止、例外の受け渡し、安全なログ、engine・SDKへ渡す設定だけを残す。本体の再投入UUIDテストはローカルの再投入ケースへ集約し、通常の本体テストにはRedis・Outbox不使用の独立した保証を残す。期間境界・件数上限・送信結果分類の条件表は既存の部品テストが所有し、ここで繰り返さない。
