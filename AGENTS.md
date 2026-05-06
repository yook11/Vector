# Vector — プロジェクト憲法

海外テックニュース収集・AI翻訳・投資分析ダッシュボード。

## 技術スタック

- Frontend: Next.js 16 (App Router, TypeScript, Tailwind CSS, shadcn/ui, Biome)
- Backend: FastAPI (Python 3.13+, SQLModel, Pydantic v2)
- Database: PostgreSQL 16 (Alembic マイグレーション)
- AI: Gemini API (抽象化済み、差し替え可能)
- インフラ: Docker Compose

## パッケージ管理
- Backend: uv add でパッケージ追加（pip install は使わない）
- Frontend: npm install でパッケージ追加

## ワークフロー

- 検証は `/review` スキルを実行すること

## リサーチ義務

ライブラリのAPIに確信が持てない場合、推測でコードを書かず `/research` スキルを使うこと。
信頼できる情報源は各ディレクトリの `AGENTS.md` と `/research` スキルに定義済み。

## 開発ルール

### 命名規約（レイヤー間の対応）

| レイヤー | 規約 | 例 |
|---|---|---|
| DB (SQLModel) | snake_case | `news_article_id` |
| API (JSON) | camelCase | `newsArticleId` |
| TypeScript | camelCase | `newsArticleId` |

### コメント言語
- ドックストリング・説明コメント（`#`）は日本語
- 機能的コメント（`# type: ignore`, `# noqa`, `# TODO:` 等）・実装識別子・エラー/ログメッセージは英語

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
- 新規 uv add パッケージ追加
- APIレスポンス形式の破壊的変更

### Never do
- `.env` の読取・表示・編集、秘匿値のハードコード
- 古いAPIパターン使用（Pydantic v1, Pages Router, SQLAlchemy同期）
- 認証ロジックのバイパス・簡略化
- テスト通過のための機能削除・無効化
- SSoT（Pydantic schemas）と矛盾するAPIレスポンスの実装

## サブエージェントへの指示方針

- 対象ディレクトリとその AGENTS.md を明示、必要な文脈は plan ファイルや指示で提供する