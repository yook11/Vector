# backend/tests/ — テストガイド

バックエンドの全テストをここに配置する。pytest + pytest-asyncio + httpx (AsyncClient) を使用。

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

### カバレッジ
- サービス層: 主要パス + エラーケース
- ルーター: 正常系 + 404/409 等のエラーレスポンス
- モデル: バリデーションの境界値
