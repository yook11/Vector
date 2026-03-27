# Vector — プロジェクト憲法

海外テックニュース収集・AI翻訳・投資分析ダッシュボード。

## 技術スタック

- Frontend: Next.js 16 (App Router, TypeScript, Tailwind CSS, shadcn/ui, Biome)
- Backend: FastAPI (Python 3.12+, SQLModel, Pydantic v2)
- Database: PostgreSQL 16 (Alembic マイグレーション)
- AI: Gemini API (抽象化済み、差し替え可能)
- インフラ: Docker Compose

## パッケージ管理
- パッケージ追加後は `requirements.txt`（Backend）/ `package.json`（Frontend）も更新すること

## 設計ドキュメント（必要時に参照）

タスクに応じて該当ドキュメントを読むこと。全部一度に読む必要はない。

| ドキュメント | 参照タイミング |
|---|---|
| `docs/00_PROJECT_OVERVIEW.md` | プロジェクト全体像を把握したい時 |
| `docs/01_DIRECTORY_STRUCTURE.md` | ファイルの置き場所に迷った時 |
| `docs/02_DATABASE_DESIGN.md` | DB関連の作業時 |
| `docs/04_API_SPECIFICATION.md` | API実装・フロント実装時 |
| `docs/05_PHASE2_PLAN.md` | Phase 2 の計画確認時 |
| `docs/05b_TASKQUEUE_POC_REPORT.md` | タスクキュー関連の作業時 |

## ワークフロー

- 検証は `/review` スキルを実行すること

## リサーチ義務

ライブラリのAPIに確信が持てない場合、推測でコードを書かず `/research` スキルを使うこと。
信頼できる情報源は各ディレクトリの `CLAUDE.md` と `/research` スキルに定義済み。

## 開発ルール

### 命名規約（レイヤー間の対応）

| レイヤー | 規約 | 例 |
|---|---|---|
| DB (SQLModel) | snake_case | `news_article_id` |
| API (JSON) | camelCase | `newsArticleId` |
| TypeScript | camelCase | `newsArticleId` |

### APIスキーマ管理
- **SSoT は FastAPI の Pydantic schemas** — 型生成は `/gen-types` スキルを使用

### ブランチ戦略
- `main` → プロダクション / `develop` → 開発統合 / `feature/*` → 機能開発

### 環境変数
- `.env` に集約、コードでは `backend/app/config.py` 経由のみでアクセス
- `.env.example` を参照し、直接 `os.environ` は使わないこと

## AIエージェント行動境界

### Always do
- 実装完了前に `ruff check` + `pytest` を実行、エラーは自己修正
- 全関数に型ヒント付与
- DB変更は Alembic マイグレーション経由のみ

### Ask first
- SQLModelモデル変更・DBスキーマ変更
- 新規 pip パッケージ追加
- APIレスポンス形式の破壊的変更

### Never do
- `.env` の読取・表示・編集、秘匿値のハードコード
- 古いAPIパターン使用（Pydantic v1, Pages Router, SQLAlchemy同期）
- 認証ロジックのバイパス・簡略化
- テスト通過のための機能削除・無効化
- SSoT（Pydantic schemas）と矛盾するAPIレスポンスの実装

## サブエージェントへの指示方針

- 対象ディレクトリとその CLAUDE.md を明示、設計ドキュメントは1〜2個に絞る

## 開発の始め方

プロジェクト全体像は `docs/00_PROJECT_OVERVIEW.md` を参照。