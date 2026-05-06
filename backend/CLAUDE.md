# backend/ — FastAPI バックエンド

FastAPI + Python 3.12 + SQLAlchemy 2.0 による非同期APIサーバー。

## API設計

- SSoT は `app/schemas/` の Pydantic モデル → FastAPI が `/openapi.json` を自動生成 → フロントエンドの型は `npm run generate-types` で自動生成

## 禁止事項（NEVER）

1. **NEVER** `os.environ` を直接参照してはならない → `app/config.py` 経由のみ
2. **NEVER** Alembic を経由せずにDBスキーマを変更してはならない
3. **NEVER** Pydantic v1 の構文（`validator`, `root_validator`, `Config` class）を使ってはならない → v2 の `field_validator`, `model_validator`, `model_config` を使うこと
4. **NEVER** SQLAlchemy 1.x スタイル（`session.query()`, `Column()`）を使ってはならない → 2.0 スタイル（`select()`, `mapped_column()`）を使うこと
5. **NEVER** SQLクエリを文字列結合で構築してはならない → SQLModel/SQLAlchemy のクエリビルダーを使うこと
6. **NEVER** グローバルシングルトンで依存性を管理してはならない → FastAPI の DI (`Depends`) を使うこと

## 検証コマンド

```bash
# タスク完了前に必ず実行
uv run ruff check app/
uv run ruff format --check app/
uv run pytest tests/ -x -q

# 特定ファイルのみテスト
uv run pytest tests/test_<対象>.py -x -v
```
