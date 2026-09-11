# backend/tests/ — テストガイド

通常の単体・結合テストをここに配置し、migration適用済みDBを使うテストは`backend/local_tests/`に配置する。pytest + pytest-asyncio + httpx (AsyncClient) を使用。

## テストルール

### 全般
- テスト関数名は `test_` プレフィックス + 何をテストしているか明示
  - 例: `test_fetch_news_skips_duplicate_urls`
- 非同期テストには `@pytest.mark.asyncio` を付与
- なんのテストをしているのかわかるように簡潔にコメントを書くこと

### フィクスチャ (conftest.py)
- `setup_db` (autouse): integration テストのみ各テスト前に全テーブルを `TRUNCATE`。`auth."user"` を seed (unit テストは DDL を流さない)
- `session_factory`: Service クラステスト用の `async_sessionmaker`
- `test_database_url`: 現在の pytest worker 専用テスト DB URL
- `db_session`: テスト用 AsyncSession (`expire_on_commit=False`)
- `client`: DI でセッション差し替え済みの未認証 httpx.AsyncClient
- `auth_headers`: 通常ユーザー用 BFF プロキシ認証ヘッダー
- `authed_client`: 通常ユーザー認証済み httpx.AsyncClient
- `admin_client`: 管理者 (role=admin) 認証済み httpx.AsyncClient
- `sample_categories`: Category 3件 (ai / computing / semiconductor)
- `sample_source`: RSS ニュースソース
- `sample_hn_source`: Hacker News API ソース
- `sample_av_source`: Alpha Vantage API ソース
- テストDBは db-test 上の `vector_test` (直列) / `vector_test_gwN` (xdist) を使用 (conftest が migration role で worker ごとに初期化)

### モック方針
- 外部API（Gemini, RSS取得）は必ずモック
- `unittest.mock.AsyncMock` または `pytest-mock` を使用
- DB操作はモックせず、テストDBに対して実行

### IAM接続を使う実DBテスト

- `tests.iam_fixtures.inject_test_db_signer(monkeypatch, test_database_url)`で共通SDK client取得口を差し替え、返されたパスワードなしURLをSettingsへ渡す。
- 呼び出し単位でSDK clientを作るConsumerは`resources_module=対象のresourcesモジュール`を指定する。
- IAM設定を有効にし、製品のtoken provider・Engine・Sessionを使う；共有ヘルパーは署名結果だけをテストDBのパスワードへ置き換える。
- 通常のパスワード認証や署名自体を検証する単体テストには一律適用しない。
- IAM認証の実環境での成立はAWS試験で確認する；保証範囲は`specs/pipeline/local-db-iam-test-support.md`を参照する。

### migration適用済みDBでのシステムテスト

- `backend/local_tests/`の共通`system_database` fixtureを利用する（実行は`make test-local`）。
- このディレクトリの`setup_db`はモデルからschemaを作り直すため、システムテストへ流用しない。
- 共通基盤の使い方と権限の境界は`backend/local_tests/README.md`を参照する。

### カバレッジ
- サービス層: 主要パス + エラーケース
- ルーター: 正常系 + 404/409 等のエラーレスポンス
- モデル: バリデーションの境界値

### テストの分類

- `local_tests/`には実環境でしか確認できないテストを置き、共通DBを使うだけでシステムテストと呼ばない。
- DBの権限は`local_tests/test_database_permissions.py`で許可一覧から照合し、独自接続や環境不足のskipを追加しない。実DBの分離・回収は`local_tests/test_database_isolation.py`で確認する。
- DB不要の接続URL・準備処理の単体テストは`tests/test_local_database.py`に置く。
- migration到達・必要テーブルの存在は共通DBの準備条件として確認する。
- 業務のシステムテストは`local_tests/<工程名>/`に配置し、入力から保存・応答までの保証範囲を明示する。
- ファイルが少ない段階で分類だけの階層を追加しない。
