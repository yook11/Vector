# プロジェクト概要

## Vector とは

海外のテックニュースを自動収集・AI翻訳・投資分析するWebプラットフォーム。
次世代コンピューティング、マテリアル・インフォマティクスなど、日本では情報が少ない先端分野に特化。

## 技術スタック

| レイヤー | 技術 | 備考 |
|---------|------|------|
| Frontend | Next.js 14+ (App Router) + TypeScript | Tailwind CSS + shadcn/ui |
| Backend | FastAPI (Python 3.12+) | 非同期処理、Pydantic v2 |
| 認証 | NextAuth.js + JWT | アクセストークン + リフレッシュトークンローテーション |
| ORM | SQLModel | SQLAlchemy + Pydantic のハイブリッド |
| Database | PostgreSQL 16 + pgvector | Alembicでマイグレーション管理 |
| AI API | Gemini API（メイン） | 抽象化して差し替え可能に |
| Embedding | Gemini Embedding API | text-embedding-004, 768次元 |
| ニュース取得 | feedparser + httpx | Google News RSS + 個別サイトRSS |
| 記事抽出 | newspaper4k | 全文取得・解析 |
| タスクキュー | taskiq + Redis | 定期実行・非同期タスク処理 |
| コンテナ | Docker Compose | 開発環境の統一 |
| CI/CD | GitHub Actions | lint, test, build |

## フェーズ分け

### Phase 1 — MVP（完了）
- ニュース自動取得（RSS）
- AI翻訳・要約・センチメント分析
- ダッシュボード表示
- キーワード管理
- Docker Compose で一発起動

### Phase 2 — 本格化（完了）
- NextAuth.js による認証
- ユーザーごとのキーワードサブスクリプション・ウォッチリスト
- taskiq + Redis によるタスクキュー分離
- pgvector によるセマンティック検索・類似記事推薦
- 記事の全文取得・分析（newspaper4k）
- Gemini Embedding による記事ベクトル化

### Phase 3 — 公開
- Vercel + Railway / Fly.io デプロイ
- レート制限・課金プラン
- 通知機能（メール / LINE）
- PWA対応
