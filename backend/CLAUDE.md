# backend/ — FastAPI バックエンド

FastAPI + Python 3.13 + SQLModel (SQLAlchemy 2.0 async) による非同期APIサーバー。

## API設計

- SSoT は `app/schemas/` の Pydantic モデル → FastAPI が `/openapi.json` を自動生成 → フロントエンドの型は `npm run generate-types` で自動生成

## モジュール構成 (bounded context)

- `app/collection/` — ニュース収集 (ingestion / extraction / fetcher 群)
- `app/analysis/` — 記事単位の AI 分析 (assessor / embedder / extractor)
- `app/insights/` — 集約 AI: snapshot (weekly_trends) / briefing (weekly_briefing)
- `app/digest/` — 週次トレンドダイジェスト pipeline
- `app/audit/` — `pipeline_events` 監査基盤 (Discriminated Union payload / per-stage semantic API in `stages/`)
- `app/search/` — semantic search + per-user 1 日 quota
- `app/routers/` — REST endpoint
- `app/schemas/` — API SSoT (Pydantic v2 / FastAPI 自動 OpenAPI)
- `app/models/` — DB SSoT (SQLModel + Alembic migration)
- `app/queue/` — Pure DI composition root (taskiq broker / scheduler / AI provider 配線) + 全 cron task / back-fill helper / kiq message DTO

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
