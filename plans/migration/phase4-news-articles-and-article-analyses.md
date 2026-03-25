# Phase 4: news_articles リファクタリング + article_analyses 新設計

> 作成日: 2026-03-25
> ブランチ: feature/better-auth (既存)
> スペック: `specs/db-redesign.md/news-articles-table.md`, `specs/db-redesign.md/article-analyses-table.md`

## 概要

news_articles テーブルのカラムリネーム・削除・制約変更と、analyses テーブルの article_analyses への再構築を同時に行う。

### 方針

- **news_articles（既存テーブル）**: 追加(NULLABLE) → データ移行 → 制約締め → コード切替 → 旧カラム削除
- **article_analyses（新テーブル）**: 最初から全制約付きで作成 → データ移行 → コード切替 → 旧テーブル削除

---

## Step 0: データ事前チェック（手動SQL） ✅ 完了

全て確認済み（2026-03-25）。

| チェック項目 | 結果 |
|-------------|------|
| `source_id IS NULL` の行数 | **0** — NOT NULL 化に問題なし |
| `LENGTH(description_original) > 2000` | **0** — VARCHAR(2000) 化に問題なし |
| 1記事に複数分析 | **0件** — 1:1 移行に問題なし |
| `analysis_translations` の locale 種類 | **ja のみ** |
| `analyses.reasoning IS NULL` の行数 | **1件** — 該当行を削除する |
| `ai_models` の内容 | `id=1, gemini, gemini-2.5-flash-lite` |
| `impact_score` の分布 | 1:3, 2:38, 3:144, 4:130, 5:106, 6:302, 7:504, 8:226, 9:22 |

### 事前データ修正（Step 1 の前に手動実行）

```sql
-- reasoning が NULL の分析とその関連データを削除
DELETE FROM analysis_translations
WHERE analysis_id IN (SELECT id FROM analyses WHERE reasoning IS NULL);

DELETE FROM analyses WHERE reasoning IS NULL;
```

---

## Step 1: news_articles 新カラム追加 + article_analyses テーブル作成

**Alembic マイグレーション 1本目**

既存のデータ・コードに影響ゼロ。

### 1-A: news_articles に新カラム追加（NULLABLE）

既存データがあるため、この時点では NULLABLE で追加。制約は Step 3 で締める。

| 新カラム | 型 | 制約（この時点） |
|---------|-----|----------------|
| `original_title` | VARCHAR(500) | NULLABLE |
| `original_url` | VARCHAR(2048) | NULLABLE |
| `original_content` | TEXT | NULLABLE |
| `news_source_id` | INTEGER | NULLABLE |
| `created_at` | TIMESTAMPTZ | NULLABLE |

### 1-B: article_analyses テーブル新規作成（全制約付き）

新テーブルのため、最初から本番仕様の制約・インデックスを設定する。

| カラム | 型 | 制約 |
|--------|-----|------|
| `id` | SERIAL | PRIMARY KEY |
| `news_article_id` | INTEGER | NOT NULL, UNIQUE, FK → news_articles(id) ON DELETE CASCADE |
| `translated_title` | VARCHAR(500) | NOT NULL |
| `summary` | TEXT | NOT NULL |
| `impact_level` | VARCHAR(20) | NOT NULL, CHECK (impact_level IN ('low', 'medium', 'high', 'critical')) |
| `reasoning` | TEXT | NOT NULL |
| `ai_model` | VARCHAR(100) | NOT NULL |
| `analyzed_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() |
| `embedding` | VECTOR(768) | NULLABLE |
| `embedding_model` | VARCHAR(100) | NULLABLE |

**インデックス:**

| インデックス | 対象 |
|-------------|------|
| UNIQUE | `news_article_id`（1:1 保証） |
| HNSW | `embedding` (vector_cosine_ops) |

---

## Step 2: データ移行

**Alembic マイグレーション 2本目（`op.execute` で SQL 実行）**

### 2-A: news_articles カラムコピー

```sql
UPDATE news_articles SET
  original_title   = title_original,
  original_url     = url,
  original_content = content,
  news_source_id   = source_id,
  created_at       = fetched_at;
```

### 2-B: article_analyses へデータ移行

旧 analyses + analysis_translations + news_articles.embedding を統合して INSERT。

```sql
INSERT INTO article_analyses (
  news_article_id, translated_title, summary,
  impact_level, reasoning, ai_model, analyzed_at,
  embedding, embedding_model
)
SELECT
  a.news_article_id,
  t.title,
  t.summary,
  CASE
    WHEN a.impact_score BETWEEN 4 AND 7 THEN 'low'
    WHEN a.impact_score IN (3, 8)       THEN 'medium'
    WHEN a.impact_score IN (2, 9)       THEN 'high'
    WHEN a.impact_score IN (1, 10)      THEN 'critical'
  END,
  a.reasoning,
  m.name,
  a.analyzed_at,
  na.embedding,
  CASE WHEN na.embedding IS NOT NULL THEN 'text-embedding-004' ELSE NULL END
FROM analyses a
JOIN ai_models m ON m.id = a.ai_model_id
JOIN analysis_translations t
  ON t.analysis_id = a.id AND t.locale = 'ja'
JOIN news_articles na ON na.id = a.news_article_id;
```

> **注意**: article_analyses は NOT NULL 制約付きで作成済みのため、translated_title / summary が NULL の行は INSERT 時にエラーになる。analysis_translations が存在しない analyses 行がある場合は事前に対処が必要。Step 0 の検証で 1:1 対応を確認済み。

### impact_score → impact_level マッピング

旧 impact_score はポジティブ(高)/ネガティブ(低)の方向性、新 impact_level は影響の大きさ。
中央付近 = low、両端 = critical。

| 旧スコア | 意味 | 新レベル | 該当件数 |
|----------|------|---------|---------|
| 1 | 強くネガティブ | critical | 3 |
| 2 | ネガティブ | high | 38 |
| 3 | ややネガティブ | medium | 144 |
| 4-7 | 中立〜軽微 | low | 1,042 |
| 8 | ややポジティブ | medium | 226 |
| 9 | ポジティブ | high | 22 |
| 10 | 強くポジティブ | critical | 0 |

### ai_model 文字列フォーマット

モデル名のみを格納する（例: `gemini-2.5-flash-lite`）。
API コール時と同一文字列で、モデル名自体がプロバイダを含む（gemini-*, gpt-*, claude-*）ため識別に十分。
将来 provider/model 形式が必要になれば VARCHAR(100) の範囲内で移行可能。

### 移行後の検証SQL（手動実行）

```sql
-- news_articles: 全行にデータが入ったことを確認
SELECT COUNT(*) FROM news_articles WHERE original_title IS NULL;   -- 0
SELECT COUNT(*) FROM news_articles WHERE original_url IS NULL;     -- 0
SELECT COUNT(*) FROM news_articles WHERE news_source_id IS NULL;   -- 0
SELECT COUNT(*) FROM news_articles WHERE created_at IS NULL;       -- 0

-- article_analyses: 行数一致確認
SELECT
  (SELECT COUNT(*) FROM analyses) AS old_count,
  (SELECT COUNT(*) FROM article_analyses) AS new_count;

-- embedding 移動確認
SELECT
  (SELECT COUNT(*) FROM news_articles WHERE embedding IS NOT NULL) AS old_embedding_count,
  (SELECT COUNT(*) FROM article_analyses WHERE embedding IS NOT NULL) AS new_embedding_count;

-- impact_level の分布確認
SELECT impact_level, COUNT(*) FROM article_analyses GROUP BY impact_level ORDER BY impact_level;
```

---

## Step 3: news_articles 制約締め

**Alembic マイグレーション 3本目**

news_articles の新カラムにデータが入った後、本番仕様の制約を適用する。
（article_analyses は Step 1 で制約設定済みのため、ここでは news_articles のみ。）

| 操作 | 対象 |
|------|------|
| ALTER NOT NULL | `original_title` |
| ALTER NOT NULL | `original_url` |
| ALTER NOT NULL | `news_source_id` |
| ALTER NOT NULL | `created_at` |
| ADD UNIQUE | `original_url` |
| ADD FK RESTRICT | `news_source_id → news_sources(id) ON DELETE RESTRICT` |
| ALTER DEFAULT | `created_at DEFAULT NOW()` |
| ALTER TYPE | `description_original` TEXT → VARCHAR(2000) |
| CREATE INDEX | `idx_news_created` ON `created_at` |
| CREATE INDEX | `idx_news_source_published` ON `(news_source_id, published_at DESC)` |

---

## Step 4: コード切替

Step 3 完了後、アプリケーションコードを新スキーマに合わせて更新する。

### 4-A: モデル層

| ファイル | 変更内容 |
|---------|---------|
| `models/news.py` | NewsArticle モデルを新カラム名に変更。旧カラムは一時的に残す |
| `models/analysis.py` | AnalysisResult → ArticleAnalysis に書き換え。AnalysisTranslation 削除 |
| `models/article_group.py` | 一時的に残す（Step 5 で削除） |
| `models/ai_model.py` | 一時的に残す（Step 5 で削除） |
| `models/__init__.py` | エクスポート更新 |

### 4-B: スキーマ層

| ファイル | 変更内容 |
|---------|---------|
| `schemas/news.py` | NewsResponse を新カラム名に対応 |
| `schemas/analysis.py` | AnalysisResponse を article_analyses 構造に変更。AIModelBrief 削除 |

### 4-C: サービス層

| ファイル | 変更内容 |
|---------|---------|
| `services/ai_analyzer.py` | ArticleAnalysis に直接書き込み。ai_model_id ルックアップ削除、ai_model 文字列に変更 |
| `services/gemini_analyzer.py` | model_name を返すインターフェースは維持 |
| `services/news_fetcher.py` | 新カラム名で記事作成（original_title, original_url, news_source_id 等） |
| `services/hacker_news.py` | 同上 |
| `services/alpha_vantage.py` | 同上 |
| `services/content_extractor.py` | content → original_content。content_fetched_at / content_fetch_attempts 参照削除 |
| `services/embedding.py` | article_analyses.embedding に書き込むよう変更 |
| `services/dedup.py` | article_analyses.embedding を参照。article_group_id 関連ロジック削除 |
| `services/source_helpers.py` | fetched_at → created_at |

### 4-D: ルーター層

| ファイル | 変更内容 |
|---------|---------|
| `routers/news.py` | _build_news_response を新カラム名に。eager load を article_analyses に変更 |
| `routers/me.py` | source 参照を news_source リレーション経由に変更 |

### 4-E: タスク層

| ファイル | 変更内容 |
|---------|---------|
| `tasks/taskiq_worker.py` | 新カラム名・新テーブルに合わせてパイプライン更新 |

### 4-F: 設定

| ファイル | 変更内容 |
|---------|---------|
| `config.py` | `default_ai_model_id` / `evaluation_ai_model_id` 削除 |

### 4-G: フロントエンド

| ファイル | 変更内容 |
|---------|---------|
| `frontend/src/types/generated.ts` | `npm run generate-types` で自動再生成 |
| `frontend/src/components/news/NewsCard.tsx` | titleOriginal → originalTitle, source → newsSource 経由 |
| `frontend/src/components/news/NewsDetail.tsx` | 同上 + url → originalUrl |

---

## Step 5: 旧カラム・旧テーブル削除

**Alembic マイグレーション 4本目**

コード切替が完了し、全ての参照が新スキーマに向いていることを確認後に実行。

### news_articles から削除するカラム

| カラム | 削除理由 |
|--------|---------|
| `title_original` | → `original_title` にコピー済み |
| `url` | → `original_url` にコピー済み |
| `content` | → `original_content` にコピー済み |
| `source_id` | → `news_source_id` にコピー済み |
| `fetched_at` | → `created_at` にコピー済み |
| `source` | レガシー。news_source_id で代替済み |
| `content_fetched_at` | `original_content IS NOT NULL` で判定可能 |
| `content_fetch_attempts` | Redis キューへ移行 |
| `guid` | Redis へ移行 |
| `embedding` | → article_analyses に移動済み |
| `article_group_id` | → 将来 NewsEvent で対応。article_groups テーブルごと削除 |

### 削除するインデックス

| インデックス | 理由 |
|-------------|------|
| `idx_news_fetched` | fetched_at カラム削除に伴い |
| HNSW on `news_articles.embedding` | embedding カラム削除に伴い |
| `idx_news_articles_article_group_id` | article_group_id カラム削除に伴い |
| `idx_articles_source_published` (旧) | source_id カラム削除に伴い（新インデックスは Step 3 で作成済み） |

### 削除するテーブル

| テーブル | 理由 |
|---------|------|
| `analysis_translations` | article_analyses.translated_title / summary に統合済み |
| `analyses` | article_analyses に移行済み |
| `ai_models` | article_analyses.ai_model（文字列）に置き換え済み |
| `article_groups` | article_group_id 削除に伴い参照元なし |

### 削除する FK 制約

| 制約 | 理由 |
|------|------|
| `fk_news_articles_source_id` | source_id カラム削除に伴い |
| `fk_news_articles_article_group_id` | article_group_id カラム削除に伴い |
| `uq_analyses_article_model` | analyses テーブル削除に伴い |
| `uq_analysis_locale` | analysis_translations テーブル削除に伴い |

---

## 依存関係図

```
Step 0  データ事前チェック ✅ 完了
  │
  ▼
事前修正  reasoning=NULL の行を削除（手動SQL）
  │
  ▼
Step 1  news_articles 新カラム追加(NULLABLE)        ← コード影響ゼロ
        article_analyses 作成(全制約付き)
  │
  ▼
Step 2  データ移行（SQL）                            ← コード影響ゼロ
        → news_articles カラムコピー
        → article_analyses へ INSERT
        → 手動で検証SQL実行
  │
  ▼
Step 3  news_articles 制約締め                       ← コード影響ゼロ
  │
  ▼
Step 4  コード切替                                   ← モデル → スキーマ → サービス → ルーター → フロント
  │
  ▼
Step 5  旧カラム・旧テーブル削除                      ← コード切替完了確認後
```

## Alembic マイグレーション対応表

| マイグレーション | Step | 内容 |
|----------------|------|------|
| 1本目 | Step 1 | DDL: ADD COLUMN (NULLABLE) + CREATE TABLE (全制約付き) |
| 2本目 | Step 2 | DML: UPDATE + INSERT（op.execute） |
| 3本目 | Step 3 | DDL: news_articles の ALTER COLUMN + ADD CONSTRAINT + CREATE INDEX |
| 4本目 | Step 5 | DDL: DROP COLUMN + DROP TABLE |

Step 4（コード切替）はマイグレーションではなくアプリケーションコードの変更。

---

## 爆発半径サマリ

### バックエンド（変更が必要なファイル）

| カテゴリ | ファイル数 |
|---------|-----------|
| モデル | 4 (news.py, analysis.py, article_group.py, ai_model.py) |
| スキーマ | 2 (news.py, analysis.py) |
| サービス | 8 (ai_analyzer, gemini_analyzer, news_fetcher, hacker_news, alpha_vantage, content_extractor, embedding, dedup) |
| ルーター | 2 (news.py, me.py) |
| タスク | 1 (taskiq_worker.py) |
| 設定 | 1 (config.py) |
| **合計** | **18 ファイル** |

### フロントエンド

| カテゴリ | ファイル数 |
|---------|-----------|
| 型定義 | 1 (generated.ts — 自動再生成) |
| コンポーネント | 2 (NewsCard.tsx, NewsDetail.tsx) |
| **合計** | **3 ファイル** |
