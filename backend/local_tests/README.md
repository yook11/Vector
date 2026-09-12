# migration適用済みDBを使うローカルテスト

実行: リポジトリルートで`make test-local`。

Docker、backendの`uv sync --frozen`、frontendの`npm ci`、Node/npmが必要。
Better Auth CLIは既存のfrontend developmentイメージと同じ版をnpxで起動する。

## 配置と保証範囲

ここには、ローカルに用意した実DB環境でしか確認できないテストを置く。
DB不要の接続URL・準備処理の単体テストは通常の`tests/`に置く。

```text
local_tests/
├── database.py                   共通DBの構築・分離
├── conftest.py                   共通fixture
├── test_database_permissions.py  3ロールの許可一覧・実操作（40件）
├── test_auth_provisioning_schema.py  既存のAuthデータ契約（2件）
├── test_database_isolation.py    実DBのケース分離・失敗時の回収（2件）
├── embedding/conftest.py         共通の接続設定・HTTP境界fixture
├── embedding/support.py          記事準備・実ハンドラー呼び出し
├── embedding/test_invocation_resources.py  呼び出し単位のDB接続管理（5件）
├── embedding/test_event_processing.py  対象記事の保存成功・保存失敗（2件）
├── embedding/test_duplicate_processing.py  重複配送・同時処理での保存結果の維持（2件）
├── assessment/conftest.py        接続設定・HTTP境界・実DB障害と待機の制御
├── assessment/support.py         記事準備・実ハンドラー呼び出し・別接続からの確認
├── assessment/test_event_processing.py  対象内・対象外の確定保存と原子性（3件）
├── assessment/test_duplicate_processing.py  再配送・同じ判定区分の同時保存（4件）
└── assessment/test_invocation_resources.py  呼び出し単位のDB接続管理（5件）
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

`embedding/test_invocation_resources.py`は実際のLambda handlerからSDK・Consumer・Repository・実DBを通し、呼び出し単位のDB接続管理を確認する。
各呼び出しの観測結果を1件だけ取り出し、Engine終了直前の貸出数が0、終了処理の完了、別接続からの保存結果、DB側で使用した接続IDが消えていることを確認する。
異なる記事を順番に処理し、2回目の呼び出しでも保存と解放が成立することを確認する。
通信失敗のケースはHTTP境界でConnectErrorを発生させ、失敗応答後の未保存・貸出返却・実接続終了と、次の呼び出しの保存・解放を確認する。エラー番号や分類は網羅しない。
DB操作中の期限切れは、別接続で対象行をロックし、pg_blocking_pidsで保存処理の待機を確認してからConsumerの実timeoutをrescheduleする。ロック保持中に未保存・接続解放を確認し、ロック解除と期限操作の差し替え解除後に同じ記事を正常保存する。
AI待機中のケースはHTTPリクエスト到達の合図で応答を停止し、貸出接続0件と、実DB接続がidleかつxact_startなしであることを確認する。確認失敗時もfinallyで応答を再開して呼び出しの終了を待つ。
既存Consumerテストから接続返却の保証を移し、読み取り回数1回の保証は`test_loads_ready_facts_once`に残す。
正常終了時のDB接続解放と連続呼び出し時のDB資源管理はここで保証し、`tests/lambda_handlers/test_embedding_handler.py`ではモックによる同じ確認を重ねない。
ハンドラー側には、準備したConsumerへの接続と初期化失敗・キャンセル時の利用範囲の終了を残す。SDK・HTTP・DBそれぞれの終了順や終了処理自体の障害は、クライアントと資源管理部品のテストで確認する。
Engineとdisposeは実物を使用し、接続IDと終了時の状態だけを観測する。DB側の切断反映は最大2秒待つ。
外部境界のIAM署名・SSM取得・Gemini HTTP応答とテスト用Settingsを差し替える。DB待機テストでは元のConsumer timeoutを保持して期限の到来だけを制御する。

監査の詳細項目・エラー分類・混在バッチの部分失敗応答・SDK終了順は通常の部品テストで確認する。

Assessmentも実handler → 実DeepSeek SDK・Assessor → Consumer・Repository → 製品Engineを通す。
DBは共通のmigration適用済み環境へ`vector_app`で接続し、IAM署名・SSM取得・AI HTTP応答だけを外部境界で差し替える。
本番と同じ1接続・追加接続なし・各DB timeout 5秒を使う。AWS IAMの実認証・本番TLS・AIへの実通信は確認しない。

- `assessment/test_event_processing.py`: 対象記事の入力・対象内結果と成功監査・対応Outbox、対象外結果と成功監査だけの保存を確認する。障害ケースでは同一トランザクション内に結果・成功監査・Outboxが実INSERT済みであることを観測し、実SQLエラーを起こす。応答後の別接続から全件のロールバックと別トランザクションの失敗監査を確認する。
- `assessment/test_duplicate_processing.py`: 再配送で異なるAI応答を用意しても保存済み内容が変わらないことを確認する。同時処理では両方をAIまで進め、先行側の実INSERT後に確定を停止する。後続のINSERTが一意制約のロック待ちになったことをDBで観測してから再開し、先行側の内容だけが残り、後続が`ALREADY_ASSESSED`になることを確認する。対象内同士・対象外同士の2種類を扱い、対象内と対象外が競合するケースは保証しない。
- `assessment/test_invocation_resources.py`: 連続呼び出しでの1接続再利用と終了、AI応答待ちでの接続返却とトランザクション終了、HTTP失敗・実DB障害・DB待機中の業務期限切れ後の接続解放を確認する。期限切れは実INSERTのロック待ちを観測してから既存timeoutをrescheduleし、失敗監査後の接続終了と同じ記事の再処理を確認する。設定値をテスト用の短時間へ変えず、60秒も待たない。

Assessmentの既存Consumerテストから同時保存と接続返却の保証を移し、通常のResources実DBテストは上記の実呼び出しへ置き換えた。
通常のConsumerテストには照会回数・DB由来の監査ID・判定済み時のAIとメトリクスの非実行を残し、Serviceの重複テストは`ALREADY_ASSESSED`とcommit非実行に絞る。
成功監査・Outbox・commit各境界の失敗伝播、元例外保持、設定や資源の生成・終了順と二次障害は、引き続き通常の`tests/`が担当する。
製品コードをテスト用に変更せず、実Engineと処理は保持したまま観測・DB障害・競合の順序だけを制御する。
詳細は[Assessment仕様](../../specs/pipeline/assessment-consumer.md)を参照する。

DB不要の単体テストは`../tests/test_local_database.py`（接続URL・準備処理の後片付け、5件）と
`../tests/test_iam_fixtures.py`（共通IAM署名差し替え、11件）にある。
`tests/analysis/embedding/`に残る業務判断・エラー分岐のテストは従来のDB環境を使う。
以前の`make test-system`は`make test-local`へ改名した。

目的別に実行する場合はbackendで次を使う。

```sh
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
