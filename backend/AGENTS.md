# backend/ — FastAPI バックエンド

FastAPI + Python 3.13 + SQLAlchemy 2.0 async (Declarative / `mapped_column` + `Mapped[T]`) による非同期APIサーバー。

## API設計

- SSoT は `app/schemas/` の Pydantic モデル → FastAPI が `/openapi.json` を自動生成 → フロントエンドの型は `npm run generate-types` で自動生成

## Skills

- schema 変更・Alembic migration: `/migration`
- SQL / ORM query: `/database-queries`
- Pydantic schema 変更後の型生成: `/gen-types`

## モジュール構成

bounded context (ドメイン):
- `app/collection/` — ニュース収集 BC。stage1=取得 (`article_acquisition/`) / stage2=本文補完 (`article_completion/`) / 宣言 (`sources/`)
- `app/analysis/` — 記事単位 AI 分析。投資判定 (`assessment/`) / 本文整形 (`curation/`) / ベクトル化 (`embedding/`)
- `app/insights/` — 集約 AI。週次トレンド (`snapshot/`) / 週次ブリーフィング (`briefing/`)
- `app/audit/` — `pipeline_events` 監査基盤 (Discriminated Union payload / per-stage semantic API in `stages/`)
- `app/queue/` — Pure DI composition root (broker / scheduler / AI provider 配線) + cron task + kiq message DTO

## 禁止事項（NEVER）

1. **NEVER** `os.environ` を直接参照してはならない → `app/config.py` 経由のみ
2. **NEVER** Alembic を経由せずにDBスキーマを変更してはならない
3. **NEVER** Pydantic v1 の構文（`validator`, `root_validator`, `Config` class）を使ってはならない → v2 の `field_validator`, `model_validator`, `model_config` を使うこと
4. **NEVER** SQLAlchemy 1.x スタイル（`session.query()`, `Column()`）を使ってはならない → 2.0 スタイル（`select()`, `mapped_column()`）を使うこと
5. **NEVER** SQLクエリを文字列結合で構築してはならない → SQLAlchemy のクエリビルダーを使うこと
6. **NEVER** グローバルシングルトンで依存性を管理してはならない → FastAPI の DI (`Depends`) を使うこと

## 検証

実装変更後は `/check` スキルで、このディレクトリに該当する検証を実行する。
