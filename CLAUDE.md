# Vector — プロジェクト憲法

海外テックニュース収集・AI翻訳・投資分析ダッシュボード。

## 技術スタック

- Frontend: Next.js 14+ (App Router, TypeScript, Tailwind CSS, shadcn/ui)
- Backend: FastAPI (Python 3.12+, SQLModel, Pydantic v2)
- Database: PostgreSQL 16 (Alembic マイグレーション)
- AI: Gemini API (抽象化済み、差し替え可能)
- インフラ: Docker Compose

## パッケージ管理
- Python パッケージの追加・削除は `pip` ではなく `uv pip` を使うこと
- パッケージ追加後は `requirements.txt` も更新すること

## 設計ドキュメント（必要時に参照）

タスクに応じて該当ドキュメントを読むこと。全部一度に読む必要はない。

| ドキュメント | 参照タイミング |
|---|---|
| `docs/00_PROJECT_OVERVIEW.md` | プロジェクト全体像を把握したい時 |
| `docs/01_DIRECTORY_STRUCTURE.md` | ファイルの置き場所に迷った時 |
| `docs/02_DATABASE_DESIGN.md` | DB関連の作業時 |
| `docs/03_CLAUDE_CODE_WORKFLOW.md` | **タスク分解・サブエージェント指示の全手順** |
| `docs/04_API_SPECIFICATION.md` | API実装・フロント実装時 |
| `docs/05_PHASE2_PLAN.md` | Phase 2 の計画確認時 |
| `docs/05b_TASKQUEUE_POC_REPORT.md` | タスクキュー関連の作業時 |

## ワークフロー（Plan-First 必須）

非自明なタスクでは、以下の4フェーズを必ず経由すること。

1. **Explore** — 対象ファイルを読み、影響範囲を特定する
2. **Plan** — 変更するファイル一覧と実装手順を提示し、承認を待つ
3. **Implement** — 承認後にコーディングを開始する
4. **Verify** — 実装後、完了報告前に検証コマンドを実行する

### 検証プロトコル（タスク完了前に必ず実行）

```bash
# Backend
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Frontend
cd frontend && npx eslint src/ && npx tsc --noEmit
```

## リサーチ義務

ライブラリのAPIに確信が持てない場合、推測でコードを書かず `/research` スキルを使うこと。
信頼できる情報源は各ディレクトリの `CLAUDE.md` と `/research` スキルに定義済み。

## 開発ルール

### 言語規約
- コード中のコメント・変数名: **英語**
- CLAUDE.md・ドキュメント: **日本語**
- コミットメッセージ: **英語** (Conventional Commits、スコープ付き)
  - 例: `feat(backend): add news fetcher service`

### 命名規約（レイヤー間の対応）

| レイヤー | 規約 | 例 |
|---|---|---|
| DB (SQLModel) | snake_case | `news_article_id` |
| API (JSON) | camelCase | `newsArticleId` |
| TypeScript | camelCase | `newsArticleId` |

### APIスキーマ管理（型共有パイプライン）
- **SSoT（Single Source of Truth）は FastAPI の Pydantic schemas**
- 型共有の流れ:
  1. `backend/app/schemas/` の Pydantic モデルを定義・変更
  2. FastAPI が `/openapi.json` を自動生成
  3. `npm run generate-types` で `frontend/src/types/generated.ts` を自動生成
  4. `frontend/src/types/index.ts` で re-export + narrowing
- `generated.ts` は手動編集禁止（自動生成ファイル）

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
- 詳細は `docs/03_CLAUDE_CODE_WORKFLOW.md` を参照

## スキル一覧

| コマンド | 用途 |
|---------|------|
| `/review` | lint + テスト + 型チェックの一括検証 |
| `/db-migrate` | Alembic マイグレーション作成ワークフロー |
| `/gen-types` | Pydantic → OpenAPI → TypeScript 型生成 |
| `/research` | 公式ドキュメント調査（サブエージェント実行） |

## 開発の始め方

`docs/03_CLAUDE_CODE_WORKFLOW.md` を読み、Step 1 から順に実行すること。