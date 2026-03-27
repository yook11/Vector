# データベース設計

## スキーマ構成

PostgreSQL 16 + pgvector。`auth` と `public` の2スキーマに分離。

| スキーマ | 管理者 | 内容 |
|---------|-------|------|
| `auth` | Better Auth CLI | user, session, account, verification テーブル |
| `public` | Alembic | アプリケーション主要テーブル |

- `auth` スキーマは Alembic の autogenerate から除外（`alembic/env.py` の `include_name` で制御）
- `auth.user.id` は UUID（Phase 6a で cuid から移行）
- `auth_user_ref` モデルでアプリ側から auth.user への FK 参照を実現

## ER図

```mermaid
erDiagram
    categories {
        int id PK
        varchar slug UK "例: ai_ml, quantum"
        varchar name UK "表示名"
    }

    keywords {
        int id PK
        varchar name UK "例: ChatGPT, Quantum Computing"
        int category_id FK
        varchar status "provisional | approved | rejected"
        boolean is_ai_generated
        timestamptz approved_at
        timestamptz created_at
        timestamptz updated_at
    }

    news_sources {
        int id PK
        varchar name "ソース表示名"
        varchar source_type "rss | api"
        varchar site_url "サイトURL"
        varchar endpoint_url UK "フィード/API URL"
        boolean is_active
        timestamptz created_at
        timestamptz updated_at
    }

    news_articles {
        int id PK
        varchar original_title "元タイトル"
        varchar original_url "元URL"
        text original_content "元本文"
        varchar original_description "元説明"
        int news_source_id FK
        timestamptz published_at
        timestamptz created_at
        vector embedding "768次元 (pgvector)"
    }

    article_analyses {
        int id PK
        int news_article_id FK_UK "1:1"
        varchar translated_title "翻訳タイトル"
        text summary "AI要約"
        varchar impact_level "low | medium | high | critical"
        text reasoning "判断理由"
        varchar ai_model "使用モデル名"
        timestamptz analyzed_at
        vector embedding "768次元 (pgvector)"
        varchar embedding_model "埋め込みモデル名"
    }

    article_keywords {
        int news_article_id PK_FK
        int keyword_id PK_FK
    }

    fetch_logs {
        int id PK
        int source_id FK
        varchar status "success | error"
        int articles_count
        text error_message
        int duration_ms
        timestamptz fetched_at
    }

    watchlist_entries {
        uuid user_id PK_FK "auth.user.id"
        int news_article_id PK_FK
        timestamptz created_at
    }

    categories ||--o{ keywords : "has many"
    news_sources ||--o{ news_articles : "has many"
    news_sources ||--o{ fetch_logs : "has many"
    news_articles ||--o| article_analyses : "has one"
    news_articles ||--o{ article_keywords : "has many"
    keywords ||--o{ article_keywords : "tagged by"
    news_articles ||--o{ watchlist_entries : "watched by"
```

## テーブル詳細

### auth スキーマ（Better Auth 管理）

Better Auth CLI (`npx @better-auth/cli migrate`) が自動生成・管理するテーブル。
Alembic の管理対象外。

| テーブル | 用途 |
|---------|------|
| `auth.user` | ユーザー (id: UUID, email, name, role, etc.) |
| `auth.session` | セッション管理 |
| `auth.account` | 認証プロバイダー連携 |
| `auth.verification` | メール検証トークン |

`auth_user_ref` モデル（`backend/app/models/auth_ref.py`）により、`auth.user` テーブルへの FK 参照を
ORM レベルで管理。マイグレーション対象外（`auth` スキーマは Better Auth が管理）。

### categories

旧 `keyword_categories` + `investment_categories` を統合した単一テーブル。

| カラム | 型 | 制約 | 備考 |
|--------|-----|------|------|
| id | SERIAL | PK | |
| slug | VARCHAR(50) | NOT NULL, UNIQUE | カテゴリ識別子 (値オブジェクト: CategorySlug) |
| name | VARCHAR(50) | NOT NULL, UNIQUE | 表示名 (値オブジェクト: CategoryName) |

### keywords

| カラム | 型 | 制約 | 備考 |
|--------|-----|------|------|
| id | SERIAL | PK | |
| name | VARCHAR(100) | NOT NULL, UNIQUE | キーワード名 (値オブジェクト: KeywordName) |
| category_id | INT | NOT NULL, FK → categories.id (RESTRICT), INDEX | 所属カテゴリ |
| status | VARCHAR(20) | NOT NULL, DEFAULT 'provisional' | provisional / approved / rejected |
| is_ai_generated | BOOLEAN | NOT NULL, DEFAULT FALSE | AI自動生成フラグ |
| approved_at | TIMESTAMPTZ | NULLABLE | 承認日時 |
| created_at | TIMESTAMPTZ | NOT NULL, server_default=now() | |
| updated_at | TIMESTAMPTZ | NOT NULL, server_default=now() | |

### news_sources

Phase 3 で再設計。RSS/API の区別を `source_type` で管理し、`endpoint_url` に統一。

| カラム | 型 | 制約 | 備考 |
|--------|-----|------|------|
| id | SERIAL | PK | |
| name | VARCHAR(50) | NOT NULL | ソース表示名 |
| source_type | VARCHAR(20) | NOT NULL | rss / api |
| site_url | VARCHAR(2048) | NOT NULL | サイトURL |
| endpoint_url | VARCHAR(2048) | NOT NULL, UNIQUE | フィード/APIエンドポイントURL |
| is_active | BOOLEAN | NOT NULL, DEFAULT TRUE | 有効/無効 |
| created_at | TIMESTAMPTZ | NOT NULL, server_default=now() | |
| updated_at | TIMESTAMPTZ | NOT NULL, server_default=now() | |

### news_articles

Phase 4 で `original_*` カラムに移行。レガシーカラムは Phase 5 で削除予定。

| カラム | 型 | 制約 | 備考 |
|--------|-----|------|------|
| id | SERIAL | PK | |
| original_title | VARCHAR(500) | NOT NULL | 元タイトル |
| original_url | VARCHAR(2048) | NOT NULL | 元URL |
| original_content | TEXT | NULLABLE | 元本文（trafilatura で取得） |
| original_description | VARCHAR(2000) | NULLABLE | 元説明 |
| news_source_id | INT | NOT NULL, FK → news_sources.id (RESTRICT) | ニュースソース |
| published_at | TIMESTAMPTZ | NULLABLE | 記事公開日時 |
| created_at | TIMESTAMPTZ | NOT NULL, server_default=now() | 取得日時 |
| embedding | vector(768) | NULLABLE | pgvector ベクトル（Gemini Embedding） |

レガシーカラム（Phase 5 削除予定）:

| カラム | 型 | 備考 |
|--------|-----|------|
| title_original | VARCHAR(500) | → original_title に移行済み |
| url | VARCHAR(2048) | → original_url に移行済み |
| source | VARCHAR(100) | → news_source_id に移行済み |
| fetched_at | TIMESTAMPTZ | → created_at に移行済み |
| content | TEXT | → original_content に移行済み |
| content_fetched_at | TIMESTAMPTZ | 廃止 |
| content_fetch_attempts | INT | 廃止 |
| source_id | INT | → news_source_id に移行済み |
| guid | VARCHAR(2048) | 廃止 |
| article_group_id | INT | 廃止（article_groups テーブルごと削除） |

インデックス:
- `idx_news_published` on `published_at`
- HNSW index on `embedding` (`vector_cosine_ops`)

### article_analyses

Phase 4 で新設。旧 `analyses` + `analysis_translations` を統合。記事と 1:1 の関係。

| カラム | 型 | 制約 | 備考 |
|--------|-----|------|------|
| id | SERIAL | PK | |
| news_article_id | INT | NOT NULL, FK → news_articles.id (CASCADE), UNIQUE | 記事ID（1:1） |
| translated_title | VARCHAR(500) | NOT NULL | 翻訳タイトル |
| summary | TEXT | NOT NULL | AI要約 |
| impact_level | VARCHAR(20) | NOT NULL | low / medium / high / critical |
| reasoning | TEXT | NOT NULL | 判断理由 |
| ai_model | VARCHAR(100) | NOT NULL | 使用AIモデル名 |
| analyzed_at | TIMESTAMPTZ | NOT NULL, server_default=now() | 分析実行日時 |
| embedding | vector(768) | NULLABLE | ベクトル埋め込み |
| embedding_model | VARCHAR(100) | NULLABLE | 埋め込みモデル名 |

### article_keywords（中間テーブル）

旧 `news_keywords` をリネーム。サロゲートキーを廃止し複合 PK に変更。

| カラム | 型 | 制約 | 備考 |
|--------|-----|------|------|
| news_article_id | INT | PK(複合), FK → news_articles.id (CASCADE) | |
| keyword_id | INT | PK(複合), FK → keywords.id (CASCADE) | |

### fetch_logs

| カラム | 型 | 制約 | 備考 |
|--------|-----|------|------|
| id | SERIAL | PK | |
| source_id | INT | NOT NULL, FK → news_sources.id, INDEX | ニュースソース |
| status | VARCHAR(20) | NOT NULL | success / error |
| articles_count | INT | NOT NULL, DEFAULT 0 | 取得記事数 |
| error_message | TEXT | NULLABLE | エラー詳細 |
| duration_ms | INT | NULLABLE | 処理時間（ミリ秒） |
| fetched_at | TIMESTAMPTZ | NOT NULL, server_default=now() | 取得日時 |

インデックス:
- `ix_fetch_logs_source_id_fetched_at` on `(source_id, fetched_at)`

### watchlist_entries

Phase 6b で旧 `watchlists` から移行。サロゲートキーを廃止し複合 PK に変更。user_id は UUID。

| カラム | 型 | 制約 | 備考 |
|--------|-----|------|------|
| user_id | UUID | PK(複合), FK → auth.user.id (CASCADE) | Better Auth ユーザーID |
| news_article_id | INT | PK(複合), FK → news_articles.id (CASCADE) | 記事ID |
| created_at | TIMESTAMPTZ | NOT NULL, server_default=now() | 登録日時 |

### レガシーテーブル（Phase 5 で削除予定）

| テーブル | 後継 | 状態 |
|---------|------|------|
| `ai_models` | `article_analyses.ai_model` (文字列) | コードでは不使用 |
| `article_groups` | 廃止予定 | コードでは不使用 |
| `analyses` | `article_analyses` | データ移行済み |
| `analysis_translations` | `article_analyses` に統合 | データ移行済み |
| `investment_categories` + 翻訳 + リンク | `categories` に統合 | データ移行済み |
| `keyword_categories` + 翻訳 + リンク | `categories` に統合 | データ移行済み |
| `news_keywords` | `article_keywords` | リネーム済み |
| `watchlists` | `watchlist_entries` | 移行済み |
| `user_keyword_subscriptions` | 削除 | 機能廃止 |
| `users` / `refresh_tokens` | `auth.user` / `auth.session` | Better Auth 移行済み |

## 設計パターン

### 認証（PG スキーマ分離）
Better Auth が `auth` スキーマでユーザー・セッションを管理。
アプリ側テーブル（`watchlist_entries`）は `user_id: UUID` で `auth.user.id` を FK 参照。
`auth_user_ref` モデルが ORM レベルでの参照を仲介する。

### 値オブジェクト（DDD）
`categories.slug` → `CategorySlug`、`categories.name` → `CategoryName`、`keywords.name` → `KeywordName` の
各値オブジェクトが不変条件を保証。Pydantic カスタムシリアライゼーション対応済み。
詳細は `specs/design-principles.md` を参照。

### 1:1 分離（article_analyses）
記事データ（news_articles）と AI 分析結果（article_analyses）を分離。
`news_article_id` の UNIQUE 制約で 1:1 を保証。
分析の再実行・差し替えが記事データに影響しない設計。

### ソース管理
`news_sources` は `source_type` カラムで RSS / API を識別。
`endpoint_url` にフィードURLまたはAPIエンドポイントを統一格納。

### ベクトル検索
pgvector 拡張で 768次元の Gemini Embedding を保持。
HNSW インデックス（cosine similarity）で類似記事検索に対応。
ベクトルは `article_analyses.embedding` に格納（`news_articles.embedding` はレガシー）。

### 重複記事グループ化（レガシー）
`article_groups` テーブルで重複記事をグループ化していたが、Phase 5 で廃止予定。
cosine distance ベースの dedup ロジック自体は残る可能性あり。

## マイグレーション

### 方針
- Alembic autogenerate で初期マイグレーション作成
- 手動で内容を確認してからコミット
- ダウングレードも必ず書く
- テストDBは `vector_test` を使用
- DBイメージは `pgvector/pgvector:pg16`（pgvector拡張が必要）
- `auth` スキーマは Alembic の管理対象外（`include_name` で除外）

### マイグレーション履歴

| # | リビジョン | 内容 |
|---|-----------|------|
| 1 | `b751d5bc7311` | 初期テーブル: keywords, news_articles, analyses, news_keywords |
| 2 | `e54c3f7851ce` | タイムスタンプを TIMESTAMPTZ に変換 |
| 3 | `2d02a83aa90f` | users, refresh_tokens テーブル追加 |
| 4 | `dc3cc7a3c587` | user_keyword_subscriptions, watchlists テーブル追加 |
| 5 | `3a9bf03a0b5f` | news_articles に content, content_fetched_at カラム追加 |
| 6 | `4bf262125474` | pgvector拡張有効化 + embedding vector(768) + HNSWインデックス |
| 7 | `a1b2c3d4e5f6` | refresh_tokens に revoked_at カラム追加 |
| 8 | `f1a2b3c4d5e6` | investment_categories テーブル追加（6カテゴリ seed） |
| 9 | `g2b3c4d5e6f7` | keyword_categories, 翻訳テーブル追加、keywords.category/is_active 削除 |
| 10 | `h3c4d5e6f7g8` | analysis_translations 追加、analyses から title_ja/summary_ja/key_topics 削除 |
| 11 | `4bda779a1d5e` | news_articles に content_fetch_attempts カラム追加 |
| 12 | `f52d4ecebe6b` | 72キーワード + カテゴリリンクのシードデータ投入 |
| 13 | `a1` | news_sources テーブル追加（CHECK制約・部分インデックス付き） |
| 14 | `a2` | news_articles に source_id, guid カラム追加 |
| 15 | `a3` | 初期 RSS フィード 7件のシードデータ投入 |
| 16 | `a4` | news_sources.category_id カラム削除 |
| 17 | `a5` | ai_models テーブル追加、analyses を正規化（ai_model_id FK） |
| 18 | `a6` | fetch_logs テーブル追加（複合インデックス） |
| 19 | `a7` | デフォルトAIモデルのシードデータ投入 |
| 20 | `a8` | article_groups テーブル追加、news_articles に article_group_id 追加 |
| 21 | `a9` | users テーブルに role カラム追加 |
| 22 | `b1` | Better Auth 移行: auth スキーマ作成、user_id INT→VARCHAR(32)、users/refresh_tokens 削除 |
| 23 | `c1` | keyword_categories → categories にリネーム・統合 |
| 24 | `c2` | keywords テーブル再設計 + article_keywords（複合PK） |
| 25 | `c3` | news_sources テーブル再設計（Phase 3） |
| 26 | `c4` | Phase 4 Step 1: news_articles 新カラム + article_analyses テーブル作成 |
| 27 | `c5` | Phase 4 Step 2: データマイグレーション（旧→新カラム） |
| 28 | `c6` | Phase 4 Step 3: news_articles 新カラムの制約強化 |
| 29 | `c6b` | description_original → original_description リネーム |
| 30 | `c7` | watchlists 削除 → watchlist_entries（複合PK, UUID user_id）作成 |
