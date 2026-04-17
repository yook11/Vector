# Topic Tagging — 変更の全体マップ

## 影響範囲の概観

```
                        ┌─────────────────────────────────┐
                        │         AI Pipeline             │
                        │  プロンプト変更                   │
                        │  category + topic を同時生成      │
                        │  正規化 + Topic 作成/検索         │
                        └──────────┬──────────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
   ┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
   │  Data Model   │     │  Service / Repo   │     │    Frontend      │
   │               │     │                    │     │                  │
   │ + topics      │     │ articles.py        │     │ NewsCard         │
   │ + topic_id FK │     │ category.py        │     │ NewsDetail       │
   │ - keywords    │     │ analysis/service   │     │ CategorySidebar  │
   │ - article_kw  │     │                    │     │ URL params       │
   └──────────────┘     └──────────────────┘     └──────────────────┘
```

## 1. データモデル層

| 変更 | 内容 |
|---|---|
| **新規**: `topics` テーブル | `id`, `name`, `category_id` (FK), `created_at`. UNIQUE(name, category_id) |
| **変更**: `article_analyses` | `+ topic_id` FK 追加 (ON DELETE RESTRICT, 初期 NULL 許可 → 移行後 NOT NULL) |
| **削除予定**: `keywords` | Topic 移行完了後に削除 |
| **削除予定**: `article_keywords` | 中間テーブル不要 (M:N → 1:1) |

## 2. AI パイプライン層

| 変更 | 内容 |
|---|---|
| **プロンプト** | Category → Topic の順で生成させる (構造的 CoT) |
| **既存 Topic 提示** | カテゴリ内上位30件をプロンプトに挿入 (ガイド、制約ではない) |
| **パース** | JSON から `category`, `topic` を抽出 |
| **正規化** | 小文字化・連続空白/ハイフン統一・trim |
| **永続化** | Topic レコードの find-or-create + `article_analyses.topic_id` 設定 |
| **廃止** | keywords_by_category の候補渡し、keyword マッチング、ArticleKeyword 作成 |

## 3. スキーマ層

| 変更 | 内容 |
|---|---|
| **新規**: `TopicEmbed` | `name: TopicName` (KeywordEmbed を置き換え) |
| **新規**: `TopicStatEmbed` | `name: TopicName, article_count: int` (KeywordStatEmbed を置き換え) |
| **変更**: `ArticleBrief` / `ArticleDetail` | `keywords: list[KeywordEmbed]` → `topic: TopicEmbed | None` |
| **変更**: `CategoryDetail` | `keywords: list[KeywordStatEmbed]` → `topics: list[TopicStatEmbed]` |
| **変更**: `ArticleListParams` | `keyword` param → `topic` param |

## 4. サービス / リポジトリ層

| 変更 | 内容 |
|---|---|
| `articles.py` (repo) | keyword フィルタ → `article_analyses.topic_id` 経由の JOIN |
| `articles.py` (service) | `build_keyword_embeds()` → `build_topic_embed()` |
| `category.py` (repo) | `fetch_keyword_stats()` → `fetch_topic_stats()` |
| `category.py` (service) | KeywordStatEmbed → TopicStatEmbed のマッピング |
| `analysis/service.py` | analyze() 内で Topic の find-or-create を追加 |

## 5. ルーター層

| 変更 | 内容 |
|---|---|
| `articles.py` | query param `keyword` → `topic` |
| `categories.py` | レスポンス構造の変更 (keywords → topics) |

## 6. フロントエンド

| 変更 | 内容 |
|---|---|
| 型再生成 | `npm run generate-types` |
| `NewsCard` | `keywords` バッジ群 → 単一の `topic` バッジ |
| `NewsDetail` | 同上 |
| `CategorySidebar` | `keywords` リスト → `topics` リスト |
| URL params | `?keyword=...` → `?topic=...` |

## 7. マイグレーション

| ステップ | 内容 |
|---|---|
| Alembic 1 | `topics` テーブル作成 + `article_analyses.topic_id` 追加 (NULL 許可) |
| データ移行 | 既存記事の再分析 or 旧 keyword → topic のマッピング |
| Alembic 2 | `topic_id` NOT NULL 化 + `keywords`, `article_keywords` 削除 |
