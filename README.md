# Vector

> 海外テックニュース収集・AI翻訳・投資分析ダッシュボード

次世代コンピューティング、マテリアル・インフォマティクスなど、
日本では情報が少ない先端分野の海外ニュースを自動収集し、
AIで翻訳・要約・センチメント分析を行う投資ダッシュボード。

## Tech Stack

- **Frontend**: Next.js 14 (App Router) + TypeScript + Tailwind CSS + shadcn/ui
- **Backend**: FastAPI + Python 3.12 + SQLModel
- **Database**: PostgreSQL 16
- **AI**: Gemini API (抽象化済み、差し替え可能)
- **Infrastructure**: Docker Compose

## Getting Started

```bash
# 1. Clone & setup
cp .env.example .env
# .env の GEMINI_API_KEY を設定

# 2. Start all services
docker compose up

# 3. Access
# Frontend: http://localhost:3000
# Backend:  http://localhost:8000/docs
```

## Documentation

設計ドキュメントは `docs/` を参照:

- `docs/00_PROJECT_OVERVIEW.md` — プロジェクト概要
- `docs/01_DIRECTORY_STRUCTURE.md` — ディレクトリ構成
- `docs/02_DATABASE_DESIGN.md` — DB設計
- `docs/03_CLAUDE_CODE_WORKFLOW.md` — 開発ワークフロー
- `docs/04_API_SPECIFICATION.md` — API仕様

## Development with Claude Code

このプロジェクトは Claude Code のサブエージェント活用を前提に設計されています。
各ディレクトリに `CLAUDE.md` が配置されており、サブエージェントが独立して作業可能です。

開発の進め方は `docs/03_CLAUDE_CODE_WORKFLOW.md` を参照してください。
