# Vector — プロジェクト憲法

海外テックニュース収集・AI翻訳・投資分析ダッシュボード。

## 技術スタック

- Frontend: Next.js 14+ (App Router, TypeScript, Tailwind CSS, shadcn/ui)
- Backend: FastAPI (Python 3.12+, SQLModel, Pydantic v2)
- Database: PostgreSQL 16 (Alembic マイグレーション)
- AI: Gemini API (抽象化済み、差し替え可能)
- インフラ: Docker Compose

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

## リサーチ義務（実装前の公式ドキュメント確認）

### 原則
ライブラリのAPI・設定・パターンについて確信が持てない場合、
**推測でコードを書かず、必ず WebSearch / WebFetch で公式ドキュメントを確認**すること。
内部知識だけに頼った実装は、古いAPI仕様や非推奨パターンの混入を招く。

### 信頼できる情報源（ホワイトリスト）
以下のドメインのみを情報源として使用すること。
これ以外のドメイン（Stack Overflow、Qiita、Zenn、個人ブログ等）は参照禁止。

**Backend / Frontend の技術固有URL**
→ 各ディレクトリの `CLAUDE.md` に定義。バージョン・パスまで固定済み。

**AI / インフラ / パッケージ情報（プロジェクト共通）**
- `https://ai.google.dev/docs` — Gemini API
- `https://docs.docker.com/` — Docker
- `https://github.com/` — ソースコード、README、Issues
- `https://pypi.org/` / `https://www.npmjs.com/` — バージョン情報

### 検索委譲パターン
リサーチでメインエージェントのコンテキストウィンドウを圧迫しないよう、
以下のパターンで検索を実行すること。

1. リサーチはサブエージェント（サブタスク）として実行する
2. サブエージェントが公式ドキュメントを検索・取得する
3. **検索結果の要約のみ**をメインプロセスに報告する（HTML全文を持ち込まない）
4. メインエージェントは要約を元に実装を進める

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

## 絶対に守るべき禁止事項（NEVER）

1. **NEVER** 公式ドキュメントを確認せずに、不確実なAPI仕様を推測で実装してはならない
2. **NEVER** ホワイトリスト外の情報源（Stack Overflow、Qiita、Zenn、個人ブログ等）を根拠にコードを書いてはならない
3. **NEVER** コードの実装に着手する前に Plan フェーズを飛ばしてはならない
4. **NEVER** 検証コマンド（lint, type check, test）を実行せずに完了報告してはならない
5. **NEVER** Pydantic schemas（SSoT）と矛盾するAPIレスポンスを実装してはならない
6. **NEVER** `frontend/src/types/generated.ts` を手動編集してはならない（`npm run generate-types` で自動生成のみ）
7. **NEVER** Alembic マイグレーションなしにDBスキーマを変更してはならない
8. **NEVER** `.env` の実際の秘匿値をコードやドキュメントにハードコードしてはならない

## サブエージェントへの指示方針

1. 対象ディレクトリとそのCLAUDE.mdを必ず明示する
2. SSoTは `backend/app/schemas/` の Pydantic モデルであることを伝える
3. 必要な設計ドキュメントは**1〜2個に絞って**渡す
4. 指示テンプレートの詳細は `docs/03_CLAUDE_CODE_WORKFLOW.md` を参照

## 開発の始め方

`docs/03_CLAUDE_CODE_WORKFLOW.md` を読み、Step 1 から順に実行すること。