# backend/ — FastAPI バックエンド

## 概要

FastAPI + Python 3.12 + SQLModel による非同期APIサーバー。

## 技術スタック

- Python 3.12+
- FastAPI
- SQLModel (SQLAlchemy + Pydantic)
- Pydantic v2
- Alembic (マイグレーション)
- APScheduler (定期実行)
- httpx + feedparser (RSS取得)
- structlog (ログ)
- ruff (linter + formatter)
- pytest + pytest-asyncio (テスト)

## ディレクトリ構成

```
backend/
├── Dockerfile
├── requirements.txt
├── pyproject.toml
├── app/
│   ├── main.py              # FastAPI エントリーポイント
│   ├── config.py            # 環境変数管理 (pydantic-settings)
│   ├── db.py                # AsyncSession 設定
│   ├── dependencies.py      # FastAPI DI
│   ├── models/              # SQLModel テーブル定義
│   │   ├── keyword.py
│   │   ├── news.py
│   │   ├── analysis.py
│   │   └── associations.py
│   ├── schemas/             # Pydantic リクエスト/レスポンスモデル
│   │   ├── news.py
│   │   ├── keyword.py
│   │   └── analysis.py
│   ├── routers/             # APIエンドポイント
│   │   ├── news.py
│   │   └── keywords.py
│   ├── services/            # ビジネスロジック
│   │   ├── news_fetcher.py
│   │   ├── ai_analyzer.py   # BaseAnalyzer 抽象クラス
│   │   ├── gemini_analyzer.py
│   │   ├── openai_analyzer.py
│   │   └── scheduler.py
│   └── utils/
│       └── logger.py        # structlog 設定
├── alembic/
│   ├── env.py
│   └── versions/
└── tests/
    └── (tests/CLAUDE.md を参照)
```

## コーディングルール

### 全般
- コード中のコメント・変数名は**英語**
- ruff でフォーマット・lint チェック
- 全関数に型ヒント必須
- 非同期関数 (`async def`) をデフォルトとする

### 環境変数
- `.env` に集約し、`app/config.py` 経由でアクセス
- `pydantic-settings` の `BaseSettings` を使用
- 直接 `os.environ` を参照しない

### DB操作
- SQLModel で定義、Alembic でマイグレーション管理
- `alembic revision --autogenerate` 後に必ず内容を確認
- ダウングレードスクリプトも必ず記述
- DB カラム名は snake_case

### API設計
- レスポンスの JSON フィールドは camelCase
  - Pydantic の `model_config` で `alias_generator = to_camel`, `populate_by_name = True`
- エラーレスポンスは `{"detail": "メッセージ"}` 形式
- ルーターのプレフィックスは `/api/v1`

### ロギング
- `structlog` を使用し、`app/utils/logger.py` で設定
- 各サービスの主要処理にログ出力を入れる

### サービス層
- ルーターは薄く、ビジネスロジックは `services/` に集約
- AI分析は抽象クラス (`BaseAnalyzer`) で差し替え可能に設計
- 外部API呼び出しにはリトライとタイムアウトを設定

## 参照ドキュメント

- `docs/02_DATABASE_DESIGN.md` — テーブル設計・ER図
- `docs/04_API_SPECIFICATION.md` — API仕様
- `shared/api-schema/openapi.yaml` — APIスキーマ (Single Source of Truth)
