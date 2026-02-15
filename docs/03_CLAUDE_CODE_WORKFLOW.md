# Claude Code 開発ワークフロー & タスク分解 (v2)

## 開発の進め方

Claude Code のメインエージェントがオーケストレーターとして、
サブエージェントにタスクを委譲しながら並行開発する。

### 原則
1. **API契約ファースト**: DB設計 → APIスキーマの順で直列に定義 → フロント・バックが独立開発可能
2. **サブエージェントは独立**: 各サブエージェントは自分のCLAUDE.mdと共有スキーマだけで作業完結
3. **統合はメインが担当**: サブエージェントの成果物をメインが統合・テスト
4. **段階的統合テスト**: 各ステップ完了時に小規模な結合確認を挟む

## Phase 1 最適化フロー図

```
Step 1: 初期化
  ↓
Step 2: Docker環境構築
  ↓
Step 3: DB設計 & マイグレーション          ← 直列（先にDBを確定）
  ↓
Step 4: APIスキーマ & 型生成               ← DBモデルから導出
  ↓
  ├─── 分岐A: バックエンド ─────────────┐
  │    Step 5: API実装                   │
  │      ↓ [統合テスト①]                 │
  │    Step 6: Fetcher                   │
  │      ↓ [統合テスト②]                 │    分岐B: フロントエンド
  │    Step 7: AI Service                │    Step 9: Frontend実装
  │      ↓ [統合テスト③]                 │    (Next.js Route Handlers
  │    Step 8: Scheduler                 │     でモックAPI使用)
  │                                      │
  └──────────────┬───────────────────────┘
                 ↓
Step 10: 統合 & E2E（モック → 本番API切り替え）
```

### 並行化のポイント
- Step 3 → 4 は **直列**（整合性リスク回避）
- Step 5〜8（バックエンド）と Step 9（フロントエンド）は **並行**
- フロントは Next.js Route Handlers でモックAPIを自前で持つ（外部ツール不要）
- Step 10 でモックAPIの接続先を本番バックエンドに切り替えるだけ

## Phase 1 タスク分解

### ステップ 1: プロジェクト初期化 [メインエージェント]
```
目的: 開発環境のセットアップ
成果物:
  - リポジトリ初期化 (git init)
  - ディレクトリ構成の生成
  - 全CLAUDE.md の配置
  - .env.example の作成
  - docker-compose.yml の作成
  - .gitignore の作成
備考: この設計書群が既に配置済みなら、本ステップは完了扱い。
```

### ステップ 2: Docker環境構築 [サブエージェント: infra]
```
対象: docker-compose.yml, frontend/Dockerfile, backend/Dockerfile
CLAUDE.md: /CLAUDE.md を参照
成果物:
  - docker-compose.yml (frontend, backend, db)
  - frontend/Dockerfile (Node.js 20, Next.js dev server)
  - backend/Dockerfile (Python 3.12, FastAPI uvicorn)
  - `docker compose up` でフロント・バック・DBが起動する状態
検証:
  - localhost:3000 → Next.js デフォルトページ
  - localhost:8000/docs → FastAPI Swagger UI
  - PostgreSQL に接続可能
```

### ステップ 3: DB設計 & マイグレーション [サブエージェント: db]
```
対象: backend/app/models/, backend/alembic/
CLAUDE.md: backend/CLAUDE.md を参照
ドキュメント: docs/02_DATABASE_DESIGN.md を参照

⚠️ 重要: このステップはStep 4より先に完了させること。
SQLModelの定義がAPIスキーマの基盤になる。

成果物:
  - SQLModel テーブル定義 (4テーブル)
    - models/keyword.py    → Keyword
    - models/news.py       → NewsArticle
    - models/analysis.py   → AnalysisResult
    - models/associations.py → NewsKeyword
  - Alembic 初期マイグレーション
  - db.py (AsyncSession 設定)
検証:
  - `alembic upgrade head` でテーブル作成
  - `alembic downgrade -1` でロールバック
  - psql でテーブル・リレーション確認
```

### ステップ 4: APIスキーマ定義 [サブエージェント: schema]
```
対象: shared/api-schema/, backend/app/schemas/
CLAUDE.md: shared/CLAUDE.md を参照
ドキュメント: docs/04_API_SPECIFICATION.md を参照

⚠️ 重要: Step 3のSQLModelモデルを参照して導出すること。
命名規約:
  - DB (SQLModel): snake_case (news_article_id)
  - API レスポンス (Pydantic): camelCase (newsArticleId)
  - TypeScript: camelCase (同上)
  → Pydantic の model_config で alias_generator = to_camel を設定

成果物:
  - Pydantic リクエスト/レスポンスモデル (backend/app/schemas/)
  - OpenAPI YAML (shared/api-schema/openapi.yaml)
  - TypeScript 型定義 (shared/api-schema/types.ts)
検証:
  - openapi.yaml が valid
  - Pydantic モデルの全フィールドが openapi.yaml に存在
  - TypeScript 型が正しく生成される
```

### ステップ 5: バックエンドAPI実装 [サブエージェント: backend-api]
```
対象: backend/app/routers/, backend/app/dependencies.py, backend/app/main.py
CLAUDE.md: backend/CLAUDE.md を参照
ドキュメント: docs/04_API_SPECIFICATION.md を参照
依存: Step 3, 4

成果物:
  - routers/news.py (一覧・詳細・手動フェッチ)
  - routers/keywords.py (CRUD)
  - dependencies.py (DIコンテナ)
  - main.py (ルーター登録、CORS、ライフサイクル)
検証:
  - Swagger UI から全エンドポイントが叩ける
  - キーワードのCRUDが動く

■ 統合テスト①
  - POST /api/v1/keywords → GET で返る
  - PATCH で is_active 変更 → DELETE で 404
```

### ステップ 6: ニュース取得サービス [サブエージェント: fetcher]
```
対象: backend/app/services/news_fetcher.py
CLAUDE.md: backend/CLAUDE.md を参照
依存: Step 3, 5

成果物:
  - news_fetcher.py (RSS取得、重複チェック、DB保存)
  - tests/test_news_fetcher.py
検証:
  - "Quantum Computing" でフェッチ → DBに保存
  - URL重複 → スキップ
  - 複数キーワード → 中間テーブル正しくリンク

■ 統合テスト②
  - キーワード登録 → フェッチ → GET /api/v1/news で記事が返る
  - keyword_id フィルター・published_at ソートが動作
```

### ステップ 7: AI分析サービス [サブエージェント: ai-service]
```
対象: backend/app/services/ai_analyzer.py, gemini_analyzer.py
CLAUDE.md: backend/CLAUDE.md を参照
依存: Step 3

成果物:
  - ai_analyzer.py (BaseAnalyzer 抽象クラス)
  - gemini_analyzer.py (Gemini API 実装)
  - tests/test_ai_analyzer.py (モックAPI)
検証:
  - 英語記事 → 日本語翻訳・要約・センチメント
  - 不正JSON → エラーハンドリング
  - API失敗 → リトライ

■ 統合テスト③
  - Step 6の記事にAI分析実行
  - GET /api/v1/news/{id} で analysis フィールドあり
  - sentiment フィルター動作
```

### ステップ 8: スケジューラー [サブエージェント: scheduler]
```
対象: backend/app/services/scheduler.py, backend/app/main.py
CLAUDE.md: backend/CLAUDE.md を参照
依存: Step 6, 7

成果物:
  - scheduler.py (APScheduler AsyncIOScheduler)
  - main.py に lifespan で起動/停止
検証:
  - アプリ起動 → 定期実行でニュース取得・分析
  - ログに件数出力
```

### ステップ 9: フロントエンド実装 [サブエージェント: frontend]
```
⚠️ Step 4完了後に着手可能（Step 5〜8と並行実行）

対象: frontend/src/
CLAUDE.md: frontend/CLAUDE.md を参照
ドキュメント: docs/04_API_SPECIFICATION.md を参照
依存: Step 4 (型定義のみ)

■ モック戦略:
  src/app/api/mock/ に Next.js Route Handlers でモックAPI配置。
  lib/api-client.ts で環境変数によりモック/本番を自動切り替え:
    const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api/mock";

成果物:
  - モックAPI (src/app/api/mock/)
  - ダッシュボード (app/page.tsx)
  - キーワード設定 (app/settings/page.tsx)
  - ニュース詳細 (app/news/[id]/page.tsx)
  - 各コンポーネント
  - lib/api-client.ts
  - hooks/ (useNews, useKeywords)
検証:
  - モックAPIでダッシュボード表示
  - キーワード追加・削除
  - フィルター・ソート
```

### ステップ 10: 統合 & E2E [メインエージェント]
```
目的: モック → 本番バックエンド切り替え & 全体結合

実施:
  1. NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1 に設定
  2. docker compose up で全サービス起動
  3. E2Eフロー:
     キーワード追加 → ニュース取得 → AI分析 → 画面表示
  4. エラーケース確認
  5. モックAPI の整理
  6. README.md 作成
  7. docs/ 整備

⚠️ Step 5〜7の段階的統合テストで大部分は確認済み。
ここでは「フロント↔バック結合」と「E2Eフロー」に集中。
```

## エラー発生時のフロー

サブエージェントがエラーに遭遇した場合:
1. エラーログを収集
2. 関連するCLAUDE.mdの記述を再確認
3. 依存モジュールのインターフェースを確認
4. 解決できない場合はメインエージェントにエスカレーション

メインエージェントの対応:
1. エラーの影響範囲を特定
2. 必要であれば shared/api-schema を修正
3. 影響を受ける他のサブエージェントに変更を通知
