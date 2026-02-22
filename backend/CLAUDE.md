# backend/ — FastAPI バックエンド

FastAPI + Python 3.12 + SQLModel による非同期APIサーバー。

## 公式ドキュメント参照先（リサーチ義務）

実装においてAPI仕様に確信が持てない場合、推測でコードを書かず、
必ず以下のURLを `WebFetch` して一次情報を確認すること。

| ライブラリ | 公式URL（ここからFetchすること） | 注意点・制約 |
|---|---|---|
| FastAPI | `https://fastapi.tiangolo.com/` | 最新のプラクティスに従うこと |
| Pydantic v2 | `https://docs.pydantic.dev/latest/` | **v1構文は絶対に使用禁止。必ず latest（v2）を参照すること** |
| SQLModel | `https://sqlmodel.tiangolo.com/` | |
| SQLAlchemy 2.0 | `https://docs.sqlalchemy.org/en/20/` | **1.x スタイルの記述は禁止** |
| Alembic | `https://alembic.sqlalchemy.org/en/latest/` | |
| Taskiq | `https://taskiq-python.github.io/` | 非同期タスクキューの仕様確認用 |
| httpx | `https://www.python-httpx.org/` | |
| structlog | `https://www.structlog.org/en/stable/` | |
| pgvector-python | `https://github.com/pgvector/pgvector-python` | GitHub README が主ドキュメント |
| pytest | `https://docs.pytest.org/en/stable/` | |
| Python 3.12 | `https://docs.python.org/3/` | |

## コーディングルール

### 全般
- ruff でフォーマット・lint チェック
- 全関数に型ヒント必須、非同期関数 (`async def`) をデフォルトとする
- ルーターは薄く、ビジネスロジックは `services/` に集約

### 環境変数
- `.env` に集約し、`app/config.py` (`pydantic-settings` の `BaseSettings`) 経由でアクセス

### DB操作
- SQLModel で定義、Alembic でマイグレーション管理
- `alembic revision --autogenerate` 後に必ず生成内容を確認すること
- ダウングレードスクリプトも必ず記述
- DB カラム名は snake_case

### API設計
- **SSoT は `app/schemas/` の Pydantic モデル** → FastAPI が `/openapi.json` を自動生成
- レスポンスの JSON フィールドは camelCase（`alias_generator = to_camel`, `populate_by_name = True`）
- エラーレスポンスは `{"detail": "メッセージ"}` 形式
- ルーターのプレフィックスは `/api/v1`

### ロギング
- `structlog` を使用（`app/utils/logger.py` で設定済み）
- 各サービスの主要処理にログ出力を入れること

### サービス層
- AI分析は抽象クラス (`BaseAnalyzer`) で差し替え可能に設計
- 外部API呼び出しにはリトライ（exponential backoff）とタイムアウトを設定

## 禁止事項（NEVER）

1. **NEVER** 公式ドキュメントを確認せずに不確実なAPIの使い方を推測で書いてはならない
2. **NEVER** `os.environ` を直接参照してはならない → `app/config.py` 経由のみ
3. **NEVER** ルーター内にビジネスロジックを書いてはならない → `services/` に切り出す
4. **NEVER** Alembic を経由せずにDBスキーマを変更してはならない
5. **NEVER** 空の `except:` や `except Exception: pass` でエラーを握りつぶしてはならない
6. **NEVER** `sync` 関数でDB操作を行ってはならない → `AsyncSession` を使うこと
7. **NEVER** グローバルシングルトンで依存性を管理してはならない → FastAPI の DI (`Depends`) を使うこと
8. **NEVER** SQLクエリを文字列結合で構築してはならない → SQLModel/SQLAlchemy のクエリビルダーを使うこと
9. **NEVER** Pydantic v1 の構文（`validator`, `root_validator`, `Config` class）を使ってはならない → v2 の `field_validator`, `model_validator`, `model_config` を使うこと
10. **NEVER** SQLAlchemy 1.x スタイル（`session.query()`, `Column()`）を使ってはならない → 2.0 スタイル（`select()`, `mapped_column()`）を使うこと

## 検証コマンド

```bash
# タスク完了前に必ず実行
ruff check app/
ruff format --check app/
python -m pytest tests/ -x -q

# 特定ファイルのみテスト
python -m pytest tests/test_<対象>.py -x -v
```

## 参照ドキュメント

- `docs/02_DATABASE_DESIGN.md` — テーブル設計・ER図
- `docs/04_API_SPECIFICATION.md` — API仕様
- `docs/05b_TASKQUEUE_POC_REPORT.md` — タスクキュー設計