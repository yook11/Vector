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
└── embedding/test_duplicate_processing.py  重複配送・同時処理での保存結果の維持（2件）
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
Engineとdisposeは実物を使用し、接続IDと終了時の状態だけを観測する。DB側の切断反映は最大2秒待つ。
外部境界のIAM署名・SSM取得・Gemini HTTP応答とテスト用Settingsを差し替える。DB待機テストでは元のConsumer timeoutを保持して期限の到来だけを制御する。

監査・重複抑止・エラー分類・部分失敗応答・SDK終了順の詳細はこのテストの責務に含めない。
従来の全体動作テストをそのまま複製せず、正常終了時の保存と接続管理に絞った。

DB不要の単体テストは`../tests/test_local_database.py`（接続URL・準備処理の後片付け、5件）と
`../tests/test_iam_fixtures.py`（共通IAM署名差し替え、11件）にある。
`tests/analysis/embedding/`に残る業務判断・エラー分岐のテストは従来のDB環境を使う。
以前の`make test-system`は`make test-local`へ改名した。

目的別に実行する場合はbackendで次を使う。

```sh
uv run pytest local_tests/embedding/ -x -q
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

ローカルのDB契約・Embeddingシステムテストであり、AWS IAMの実認証・SQSの実配信・AIへの実通信・同一アプリイメージでの実行は保証しない。
詳細は[共通DB仕様](../../specs/pipeline/system-test-database.md)を参照する。

既存の`tests/test_db_user_isolation.py`の権限20件は許可一覧の照合と実操作に再編した。独自の接続先解決と環境不足によるskipは廃止し、Authの構造契約2件も共通DBで実行する。Outbox個別migrationのupgrade/downgrade試験は`tests/outbox/test_collect_permissions_migration.py`に残す。
