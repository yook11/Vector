# backend/tests/ — テストガイド

バックエンドの全テストをここに配置する。pytest + pytest-asyncio + httpx (AsyncClient) を使用。

## テストルール

### 全般
- テスト関数名は `test_` プレフィックス + 何をテストしているか明示
  - 例: `test_fetch_news_skips_duplicate_urls`
- 非同期テストには `@pytest.mark.asyncio` を付与
- 1テスト = 1アサーション（原則）

### フィクスチャ (conftest.py)
- `db_session`: テスト用 AsyncSession（テストごとにロールバック）
- `client`: httpx.AsyncClient（未認証クライアント）
- `test_user`: テスト用 User レコード
- `auth_headers`: 認証済みリクエスト用ヘッダー
- `authed_client`: 認証済み httpx.AsyncClient
- `sample_categories`: テスト用 Category レコード（ai / computing / semiconductor）
- テストDBは `vector_test` を使用

### モック方針
- 外部API（Gemini, RSS取得）は必ずモック
- `unittest.mock.AsyncMock` または `pytest-mock` を使用
- DB操作はモックせず、テストDBに対して実行

### カバレッジ
- サービス層: 主要パス + エラーケース
- ルーター: 正常系 + 404/409 等のエラーレスポンス
- モデル: バリデーションの境界値

## 参照ドキュメント

- `backend/CLAUDE.md` — バックエンド全体のルール
- `backend/app/models/` + `backend/alembic/versions/` — テーブル定義 (SQLModel + Alembic migration が SSoT)
- `backend/app/schemas/` — 期待するレスポンス形式 (Pydantic v2 が API SSoT、FastAPI が `/openapi.json` を自動生成)
