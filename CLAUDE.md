# Vector

海外テックニュース収集・AI翻訳・投資分析ダッシュボード。
次世代コンピューティング、マテリアル・インフォマティクスなど日本では情報が少ない先端分野に特化。

## 技術スタック

- Frontend: Next.js 14+ (App Router, TypeScript, Tailwind CSS, shadcn/ui)
- Backend: FastAPI (Python 3.12+, SQLModel, Pydantic v2)
- Database: PostgreSQL 16 (Alembic マイグレーション)
- AI: Gemini API (抽象化済み、差し替え可能)
- インフラ: Docker Compose

## 設計ドキュメント（必要時に参照）

タスクに応じて以下を読むこと。全部一度に読む必要はない。

| ドキュメント | 内容 | 参照タイミング |
|-------------|------|---------------|
| `docs/00_PROJECT_OVERVIEW.md` | 技術スタック・フェーズ分け | プロジェクト全体像を把握したい時 |
| `docs/01_DIRECTORY_STRUCTURE.md` | ディレクトリ構成・CLAUDE.md配置図 | ファイルの置き場所に迷った時 |
| `docs/02_DATABASE_DESIGN.md` | ER図・テーブル定義・SQLModel例 | DB関連の作業時 |
| `docs/03_CLAUDE_CODE_WORKFLOW.md` | タスク分解・実行順序・サブエージェント指示テンプレート | **最初に読むべき**。開発の進め方の全手順 |
| `docs/04_API_SPECIFICATION.md` | 全エンドポイント仕様・リクエスト/レスポンス例 | API実装・フロント実装時 |

## 開発ルール

### 言語
- コード中のコメント・変数名: 英語
- CLAUDE.md・ドキュメント: 日本語
- コミットメッセージ: 英語 (Conventional Commits)

### コミット規約
```
feat: 新機能           fix: バグ修正
docs: ドキュメント      refactor: リファクタリング
test: テスト           chore: ビルド・設定変更
```
スコープ付き例: `feat(backend): add news fetcher service`

### ブランチ戦略
- main: プロダクション
- develop: 開発統合
- feature/*: 機能開発 (例: feature/news-fetcher)

### コード品質
- Frontend: ESLint + Prettier
- Backend: ruff (linter + formatter)
- 型安全: フロント=TypeScript strict, バック=Pydantic + type hints

## サブエージェントへの指示方針

### タスク委譲時のルール
1. サブエージェントには必ず対象ディレクトリを明示する
2. `shared/api-schema/openapi.yaml` をSingle Source of Truthとして参照させる
3. 各サブエージェントは自ディレクトリの CLAUDE.md を最初に読む
4. DB操作は必ず Alembic マイグレーション経由
5. 環境変数は `.env` に集約、コードでは `backend/app/config.py` 経由でアクセス

### サブエージェントに渡すコンテキスト
タスクを委譲する際、以下を含める:
- 対象ディレクトリパス
- 関連するCLAUDE.mdのパス
- 必要な設計ドキュメントのパス（1-2個に絞る）
- 依存する他モジュールのインターフェース情報
- 期待する成果物（ファイル名、関数名など）

### 指示テンプレート
```
以下のタスクを実行してください。

■ 対象ディレクトリ: [パス]
■ 参照するCLAUDE.md: [パス]
■ 参照するドキュメント: [パス]

■ タスク:
  [具体的な実装内容]

■ 成果物:
  - [ファイル名とその責務]

■ 検証方法:
  - [テスト方法]

■ 制約:
  - 非同期関数で実装すること
  - 型ヒントを全ての関数に付けること
  - structlog でログを出力すること
  - エラーハンドリングを必ず実装し、生の例外を握りつぶさないこと
  - 実装前に、これから作成・修正するファイルの概要を1-2行で説明してからコード生成を開始すること
```

## 環境変数 (.env)
```env
# Database
DATABASE_URL=postgresql+asyncpg://vector:vector@db:5432/vector

# AI API
AI_PROVIDER=gemini
GEMINI_API_KEY=your_key
OPENAI_API_KEY=your_key

# News Fetcher
FETCH_INTERVAL_HOURS=3
MAX_ARTICLES_PER_FETCH=50

# App
FRONTEND_URL=http://localhost:3000
BACKEND_URL=http://localhost:8000
```

## 開発の始め方

`docs/03_CLAUDE_CODE_WORKFLOW.md` を読み、Step 1 から順に実行すること。