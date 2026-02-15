# backend/tests/ — テストガイド

## 概要

バックエンドの全テストをここに配置する。

## 技術スタック

- pytest
- pytest-asyncio
- httpx (AsyncClient でFastAPIの非同期テスト)

## ディレクトリ構成

```
tests/
├── CLAUDE.md
├── conftest.py              # 共通フィクスチャ
├── test_news_fetcher.py     # news_fetcher サービスのテスト
├── test_ai_analyzer.py      # AI分析サービスのテスト
└── test_routers/
    ├── test_news.py         # /api/v1/news エンドポイント
    └── test_keywords.py     # /api/v1/keywords エンドポイント
```

## テストルール

### 全般
- テスト関数名は `test_` プレフィックス + 何をテストしているか明示
  - 例: `test_fetch_news_skips_duplicate_urls`
- 非同期テストには `@pytest.mark.asyncio` を付与
- 1テスト = 1アサーション（原則）

### フィクスチャ (conftest.py)
- `db_session`: テスト用 AsyncSession（テストごとにロールバック）
- `client`: httpx.AsyncClient（FastAPI TestClient）
- `sample_keyword`: テスト用 Keyword レコード
- `sample_news_article`: テスト用 NewsArticle レコード
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
- `docs/02_DATABASE_DESIGN.md` — テーブル定義
- `docs/04_API_SPECIFICATION.md` — 期待するレスポンス形式
