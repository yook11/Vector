# プロジェクト概要

## Vector とは

海外のテックニュースを自動収集・AI翻訳・投資分析するWebプラットフォーム。
次世代コンピューティング、マテリアル・インフォマティクスなど、日本では情報が少ない先端分野に特化。

## 技術スタック

| レイヤー | 技術 | 備考 |
|---------|------|------|
| Frontend | Next.js 16 (App Router) + TypeScript | Tailwind CSS + shadcn/ui + Biome |
| Backend | FastAPI (Python 3.12+) | 非同期処理、Pydantic v2 |
| 認証 | Better Auth (BFF Proxy) | Cookie ベースセッション + BFF ヘッダー認証 |
| ORM | SQLModel | SQLAlchemy + Pydantic のハイブリッド |
| Database | PostgreSQL 16 + pgvector | Alembic マイグレーション管理、auth/public スキーマ分離 |
| AI API | Gemini API（メイン：gemini-2.5-flash-lite） | 抽象化して差し替え可能 |
| Embedding | Gemini Embedding API | 768次元ベクトル |
| ニュース取得 | feedparser + httpx | Google News RSS + Hacker News API + Alpha Vantage |
| 記事抽出 | trafilatura | 全文取得・解析 |
| タスクキュー | taskiq + Redis | 定期実行・非同期タスク処理 |
| 重複検出 | pgvector cosine distance | 記事グループ化・類似記事判定 |
| Lint/Format | Biome (Frontend) + ruff (Backend) | ESLint は廃止済み |
| コンテナ | Docker Compose | 6サービス構成 |
| CI/CD | GitHub Actions | lint, test, type check |

## アーキテクチャ概要

```
Browser
  │
  └─► Next.js Frontend / BFF (localhost:3000)
        ├── Better Auth (Cookie session, auth スキーマ in PG)
        ├── Server Components → INTERNAL_API_URL (Docker internal)
        ├── BFF Proxy (/api/proxy/*) → Backend (header auth)
        │
        └─► FastAPI Backend (Docker internal only)
              ├── Header Auth (X-User-ID / X-Internal-Secret)
              ├── News Fetcher (Google News RSS, HN API, Alpha Vantage)
              ├── AI Analyzer (Gemini API — 翻訳・要約・センチメント)
              ├── Embedding (Gemini Embedding API — pgvector)
              ├── Dedup (cosine distance — 重複記事グループ化)
              └── PostgreSQL 16 + pgvector

Redis ◄── taskiq worker (非同期タスク実行)
       ◄── taskiq scheduler (cron トリガー)
```

### 認証フロー

1. ブラウザ → Next.js BFF (`/api/auth/*`) で Better Auth セッション管理
2. BFF が Cookie からセッション検証し、内部ヘッダー (`X-User-ID`, `X-User-Role`, `X-Internal-Secret`) を付与
3. FastAPI は BFF プロキシ経由のヘッダーのみを信頼（外部からの直接アクセス不可）
4. DB は `auth` スキーマ（Better Auth テーブル）と `public` スキーマ（アプリテーブル）に分離

### Docker Compose サービス

| サービス | 役割 | ネットワーク |
|---------|------|------------|
| frontend | Next.js 16 BFF（唯一の public エントリーポイント） | public + internal |
| backend | FastAPI API サーバー | internal のみ |
| db | PostgreSQL 16 + pgvector | public + internal |
| redis | タスクキューブローカー | internal のみ |
| worker | taskiq ワーカー（ニュースパイプライン実行） | internal のみ |
| scheduler | taskiq スケジューラー（cron トリガー） | internal のみ |

## フェーズ分け

### Phase 1 — MVP（完了）
- ニュース自動取得（RSS）
- AI翻訳・要約・センチメント分析
- ダッシュボード表示
- キーワード管理
- Docker Compose で一発起動

### Phase 2 — 本格化（完了）
- ユーザーごとのキーワードサブスクリプション・ウォッチリスト
- taskiq + Redis によるタスクキュー分離
- pgvector によるセマンティック検索・類似記事推薦
- 記事の全文取得・分析（trafilatura）
- Gemini Embedding による記事ベクトル化
- 重複記事検出・グループ化
- ニュースソース管理（RSS + Hacker News API + Alpha Vantage）
- 投資カテゴリ・キーワードカテゴリの多言語対応
- Next.js 14 → 15 アップグレード

### Phase 2.5 — リファクタリング・基盤強化（進行中）

Phase 3（公開）に向けた技術基盤の刷新。DB スキーマ再設計と認証移行を軸に、DDD・セキュリティ・開発体験を改善。

- **DB スキーマ再設計**（6 段階マイグレーション）
  - Phase 0-2: カテゴリ統合、キーワードテーブル刷新
  - Phase 3: news_sources テーブル再設計
  - Phase 4: news_articles + article_analyses 分離（コード切替完了）
  - Phase 6a: Better Auth UUID 移行（コード完了、マイグレーション未実行）
  - Phase 6b: watchlists → watchlist_entries（複合 PK、コード完了、マイグレーション未実行）
- **認証移行**: NextAuth.js + JWT → Better Auth BFF Proxy（コード完了、DB マイグレーション未実行）
- **Next.js 15 → 16 アップグレード**
- **ESLint → Biome 移行**
- **DDD 値オブジェクト導入**: CategorySlug, CategoryName, KeywordName
- **XSS 対策強化**: CSP nonce ベースヘッダー、proxy middleware でのセキュリティヘッダー付与

### Phase 3 — 公開（未着手）
- Vercel + Railway / Fly.io デプロイ
- レート制限・課金プラン
- 通知機能（メール / LINE）
- PWA対応
