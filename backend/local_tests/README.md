# migration適用済みDBを使うローカルテスト

実行: リポジトリルートで`make test-local`。

`test_source_acquisition_requests.py`は、Scheduler入力から実DBの有効なソースを選定し、取得依頼一覧を作る経路を担当する。DB変更後の再選定と、cadence・登録状況による除外を確認する。対象外の理由は既存の確認結果に残り、取得依頼一覧には混ぜない。ID生成の時差同一性・入力拒否は通常の単体テストが担当し、同じ選定シナリオを重複させない。SQS送信・Lambda入口の実装時にはこの経路を拡張する。

Docker、backendの`uv sync --frozen`、frontendの`npm ci`、Node/npmが必要。
Better Auth CLIは既存のfrontend developmentイメージと同じ版をnpxで起動する。

## 配置と保証範囲

ここには、ローカルに用意した実DB環境でしか確認できないテストを置く。
DB不要の接続URL・準備処理の単体テストは通常の`tests/`に置く。

```text
local_tests/
├── database.py                   共通DBの構築・分離
├── conftest.py                   共通fixture
├── test_article_analysis_lifecycle.py  共通ライフサイクルと実DB資源管理（11件）
├── test_database_permissions.py  3ロールの許可一覧・実操作（40件）
├── test_auth_provisioning_schema.py  既存のAuthデータ契約（2件）
├── test_database_isolation.py    実DBのケース分離・失敗時の回収（2件）
├── embedding/conftest.py         共通の接続設定・HTTP境界fixture
├── embedding/support.py          記事準備・実ハンドラー呼び出し
├── embedding/test_session_boundaries.py  AI待機前のセッション返却（1件）
├── embedding/test_event_processing.py  対象記事の保存成功・保存失敗・通信失敗・DB待機期限切れ（4件）
├── embedding/test_duplicate_processing.py  重複配送・同時処理での保存結果の維持（2件）
├── assessment/conftest.py        接続設定・HTTP境界・実DB障害と待機の制御
├── assessment/support.py         記事準備・実ハンドラー呼び出し・別接続からの確認
├── assessment/test_event_processing.py  対象内・対象外の確定保存・原子性・通信失敗・DB待機期限切れ（5件）
├── assessment/test_duplicate_processing.py  再配送・同じ判定区分の同時保存（4件）
└── assessment/test_session_boundaries.py  AI待機前のセッション返却（1件）
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


## Curationの記事完成イベント配送

`curation/test_delivery.py`は取得・本文補完の両方から実Serviceで記事を保存し、実Curation relayが送信したMessageBodyを変更せず実Curation Lambdaへ渡す。共通記事完成イベントの一種類への統一、記事ID・イベントID・発生時刻・配送先の維持、対応するSignal・成功監査・Assessment向けOutboxの確定を確認する。発行元で異なる本文に異なるAI応答を返し、対象記事への保存まで照合する。

取得・補完は`vector_collect`、relay・Consumerは`vector_app`を使う。旧取得サービスの全体設定へはfixtureで非機密の値を渡し、製品のDB処理は差し替えない。RSS HTTP、記事スクレイピング、SQS通信、SSM・RDS署名、Gemini HTTPだけを外部境界で置き換える。AWSの実配送・実IAM認証は確認しない。

正常な記事完成イベントを確認する2つのServiceテストはこの経路へ集約した。非発行・ロールバックは既存Serviceテスト、配送の拒否・通信失敗はrelay統合テスト、Curation結果別の応答・入力制約・共通資源管理は既存テストが担当する。同じケースを発行元と結果の全組合せで繰り返さない。
