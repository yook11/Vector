# Phase 7c: news_articles レガシーカラム一括除去

> 作成日: 2026-03-26
> ブランチ: feature/better-auth
> 前提: Phase 7a (ArticleGroup), 7b (AIModel) 完了後
> Alembic head: `c9a1b2c3d4e5`
> **スコープ: 開発環境限定。**

## 概要

Phase 4 で新カラムに移行済みの旧カラム 9 個 + インデックスを削除する。
調査の結果、カラムの性質が 3 種類に分かれるため、サブステップに分割して段階的に実施する。

### カラム分類

| サブステップ | カラム | 性質 |
|---|---|---|
| C-1 | `title_original`, `url`, `source`, `source_id`, `fetched_at`, `embedding` | 並行書き込みのみ。クエリ・ロジック依存なし |
| C-2 | `guid` | 3 フェッチャーの dedup クエリで使用中 |
| C-3 | `content`, `content_fetched_at`, `content_fetch_attempts` | コンテンツ取得パイプラインの状態管理で使用中 |

### 削除インデックス

| インデックス | サブステップ |
|---|---|
| `idx_news_fetched` | C-1 (`fetched_at` 削除時) |

---

## C-1: 並行書き込みカラムの除去（機能変更なし）

### 対象カラム

| カラム | 移行先 | 備考 |
|---|---|---|
| `title_original` | `original_title` | リネーム済み |
| `url` | `original_url` | リネーム済み |
| `source` | `news_source.name` (FK 経由) | 非正規化カラム廃止 |
| `source_id` | `news_source_id` | FK 名変更 + NOT NULL 化済み |
| `fetched_at` | `created_at` | 統合済み |
| `embedding` | `article_analyses.embedding` | テーブル移動済み |

### C-1-1: models/news.py

- 6 カラムの Field 定義を削除
- `idx_news_fetched` を `__table_args__` から削除
- `pgvector.sqlalchemy` の `Vector` import を削除
- `source_id` 用の FK (`ondelete="SET NULL"`) 削除

### C-1-2: サービスコード（書き込み行の削除のみ）

全サービスで新旧カラムに二重書き込みしている。旧カラム書き込みを削除する。

#### services/news_fetcher.py

```python
# 削除する行:
title_original=title,
url=article_url,
source=source.name,
source_id=source.id,
```

#### services/hacker_news.py

```python
# 削除する行:
title_original=strip_html_tags(story.title)[:500],
url=story.url,
source=source.name,
source_id=source.id,
```

#### services/alpha_vantage.py

```python
# 削除する行:
title_original=strip_html_tags(item.get("title", ""))[:500],
url=url,
source=source.name,
source_id=source.id,
```

#### scripts/compare_models.py

```python
# 変更:
article.title_original  → article.original_title
NewsArticle.fetched_at.desc()  → NewsArticle.created_at.desc()
article.content  → article.original_content
title=article.title_original  → title=article.original_title
```

### C-1-3: テスト修正

`NewsArticle()` コンストラクタから以下の引数を削除:

| 引数 | 影響テストファイル |
|---|---|
| `title_original=...` | 全テストファイル (12 ファイル) |
| `url=...` | 全テストファイル |
| `source=...` | 全テストファイル |
| `source_id=...` | test_hacker_news, test_alpha_vantage, test_semantic_search |
| `fetched_at=...` | test_alpha_vantage, test_semantic_search, test_content_extractor |

また、以下のアサーションを削除:

```python
# test_news_fetcher.py
assert all(a.source == "Test Tech Source" for a in articles)  # 削除

# test_hacker_news.py
assert article.source == "Hacker News"  # 削除
```

### C-1-4: Alembic マイグレーション

`c10_phase7c1_drop_write_only_columns.py` (down_revision: `c9a1b2c3d4e5`)

#### upgrade

```sql
-- 1. FK 削除 (source_id の旧 FK)
ALTER TABLE news_articles DROP CONSTRAINT IF EXISTS news_articles_source_id_fkey;

-- 2. インデックス削除
DROP INDEX IF EXISTS idx_news_fetched;

-- 3. カラム一括削除
ALTER TABLE news_articles
    DROP COLUMN IF EXISTS title_original,
    DROP COLUMN IF EXISTS url,
    DROP COLUMN IF EXISTS source,
    DROP COLUMN IF EXISTS source_id,
    DROP COLUMN IF EXISTS fetched_at,
    DROP COLUMN IF EXISTS embedding;
```

#### downgrade

```sql
ALTER TABLE news_articles
    ADD COLUMN title_original VARCHAR(500),
    ADD COLUMN url VARCHAR(2048),
    ADD COLUMN source VARCHAR(100),
    ADD COLUMN source_id INTEGER,
    ADD COLUMN fetched_at TIMESTAMPTZ DEFAULT now(),
    ADD COLUMN embedding vector(768);

UPDATE news_articles SET
    title_original = original_title,
    url = original_url,
    source = '',
    source_id = news_source_id,
    fetched_at = created_at;

ALTER TABLE news_articles
    ALTER COLUMN title_original SET NOT NULL,
    ALTER COLUMN url SET NOT NULL,
    ALTER COLUMN source SET NOT NULL,
    ALTER COLUMN fetched_at SET NOT NULL;

CREATE INDEX idx_news_fetched ON news_articles USING btree (fetched_at);
ALTER TABLE news_articles ADD CONSTRAINT news_articles_source_id_fkey
    FOREIGN KEY (source_id) REFERENCES news_sources(id) ON DELETE SET NULL;
```

### C-1-5: 検証プロトコル

```bash
cd backend && uv run ruff check app/ && uv run ruff format --check app/ && uv run python -m pytest tests/ -x -q
```

---

## C-2: guid カラムの除去 + dedup 戦略移行

### 現状

3 つのフェッチャーサービスが `NewsArticle.guid` で dedup クエリを実行:

```python
stmt = select(NewsArticle.guid).where(NewsArticle.guid.in_(chunk))
```

同時に `original_url` による secondary dedup も実装済み。

### 移行方針

- guid-based dedup を削除し、URL-based dedup のみに統一
- `_extract_guid()` 関数は RSS entry の識別子抽出として残す（URL フォールバック用途）
- guid 値は `NewsArticle` に保存しない

### リスク

guid と URL が異なるケース（同一記事が複数ソースで異なる URL を持つ場合）で、
dedup カバレッジが低下する可能性がある。ただし:
- RSS: 大半は guid == link
- HN: URL は一意（HN の objectID は URL と 1:1 対応）
- AV: guid は URL の hash なので等価

### C-2-1: services/news_fetcher.py

```diff
- # Batch dedup: check existing guids
- guids = [g for _, g in entry_guids]
- existing_guids: set[str] = set()
- chunk_size = 500
- for i in range(0, len(guids), chunk_size):
-     chunk = guids[i : i + chunk_size]
-     stmt = select(NewsArticle.guid).where(NewsArticle.guid.in_(chunk))
-     rows = await session.execute(stmt)
-     existing_guids.update(row[0] for row in rows.all())

  # URL dedup は既存コード維持

  for entry, guid in entry_guids:
-     if guid in existing_guids:
-         result.skipped_count += 1
-         continue
      ...
      article = NewsArticle(
          ...
-         guid=guid,
      )
```

### C-2-2: services/hacker_news.py

```diff
- guids = [f"hn:{s.object_id}" for s in stories]
  urls = [s.url for s in stories]
- existing_guids: set[str] = set()
  existing_urls: set[str] = set()

- for i in range(0, len(guids), chunk_size):
-     ...select(NewsArticle.guid)...

  for story in stories:
-     guid = f"hn:{story.object_id}"
-     if guid in existing_guids:
-         result.skipped_count += 1
-         continue
      if story.url in existing_urls:
          ...
      article = NewsArticle(
          ...
-         guid=guid,
      )
```

### C-2-3: services/alpha_vantage.py

```diff
- stmt = select(NewsArticle.guid).where(NewsArticle.guid.in_(chunk))
  # URL dedup のみ維持

  article = NewsArticle(
      ...
-     guid=guid,
  )
```

### C-2-4: テスト修正

| ファイル | 変更 |
|---|---|
| test_news_fetcher.py | `guid=...` 引数削除、`a.guid` アサーション削除 |
| test_hacker_news.py | `guid=...` 引数削除、`guids = {a.guid for a in articles}` アサーション削除 |
| test_alpha_vantage.py | `guid=...` 引数削除、`a.guid.startswith("av:")` アサーション削除 |
| test_dedup.py | `_create_article_with_analysis` から guid 関連なし（影響なし） |

### C-2-5: Alembic マイグレーション

`c11_phase7c2_drop_guid.py` (down_revision: `c10...`)

#### upgrade

```sql
ALTER TABLE news_articles DROP COLUMN IF EXISTS guid;
```

#### downgrade

```sql
ALTER TABLE news_articles ADD COLUMN guid VARCHAR(2048);
CREATE UNIQUE INDEX ON news_articles (guid) WHERE guid IS NOT NULL;
```

### C-2-6: 検証プロトコル

```bash
cd backend && uv run ruff check app/ && uv run ruff format --check app/ && uv run python -m pytest tests/ -x -q
```

---

## C-3: コンテンツパイプラインカラムの除去

### 現状の使用箇所

| カラム | 使用箇所 | 用途 |
|---|---|---|
| `content` | content_extractor, news_fetcher | `original_content` と二重書き込み |
| `content_fetched_at` | taskiq_worker (クエリ), content_extractor (書き込み) | 「取得試行済み」フラグ |
| `content_fetch_attempts` | taskiq_worker (クエリ), content_extractor (書き込み) | リトライ回数制限 |

### 移行方針

`content` → `original_content` のみ使用（二重書き込み行を削除）

`content_fetched_at` + `content_fetch_attempts` の代替:

| 状態 | 現行 | 移行後 |
|---|---|---|
| 未試行 | `content_fetched_at IS NULL` | `original_content IS NULL` |
| 成功 | `content_fetched_at IS NOT NULL AND content IS NOT NULL` | `original_content IS NOT NULL AND original_content != ''` |
| 試行済み（空） | `content_fetched_at IS NOT NULL AND content IS NULL` | `original_content = ''` |
| エラー（リトライ対象） | `content_fetched_at IS NULL AND content_fetch_attempts < max` | `original_content IS NULL`（リトライ無制限） |

**注意**: `content_fetch_attempts` の除去により、リトライ回数制限が失われる。
永続的にエラーになる記事（ペイウォール等）が無限リトライされる。
対策案:
- `created_at` で古い記事を除外（例: 7日以上前はスキップ）
- 将来的に Redis に移行

### C-3-1: services/content_extractor.py

```diff
  if content is not None:
      article.original_content = content
-     article.content = content
-     article.content_fetched_at = datetime.now(UTC)
  else:
-     article.content_fetched_at = datetime.now(UTC)
+     article.original_content = ""  # mark as "tried but no content"

  if error:
-     article.content_fetch_attempts += 1
+     # leave original_content as None → will be retried
```

### C-3-2: services/news_fetcher.py

```diff
  if full_content:
      truncated = full_content[: settings.content_max_length]
      article.original_content = truncated
-     article.content = truncated
-     article.content_fetched_at = now
```

### C-3-3: tasks/taskiq_worker.py

```diff
  select(NewsArticle).where(
-     NewsArticle.content_fetched_at == None,
-     NewsArticle.content_fetch_attempts < settings.content_max_fetch_attempts,
+     NewsArticle.original_content == None,  # noqa: E711
  )
```

### C-3-4: テスト修正

| ファイル | 変更 |
|---|---|
| test_content_extractor.py | `content_fetched_at` / `content_fetch_attempts` アサーション削除・`original_content` ベースに書き換え |
| test_news_fetcher.py | `content_fetched_at` アサーション削除 |

### C-3-5: Alembic マイグレーション

`c12_phase7c3_drop_content_pipeline_columns.py` (down_revision: `c11...`)

#### upgrade

```sql
ALTER TABLE news_articles
    DROP COLUMN IF EXISTS content,
    DROP COLUMN IF EXISTS content_fetched_at,
    DROP COLUMN IF EXISTS content_fetch_attempts;
```

#### downgrade

```sql
ALTER TABLE news_articles
    ADD COLUMN content TEXT,
    ADD COLUMN content_fetched_at TIMESTAMPTZ,
    ADD COLUMN content_fetch_attempts INTEGER NOT NULL DEFAULT 0;

UPDATE news_articles SET
    content = original_content,
    content_fetched_at = CASE WHEN original_content IS NOT NULL THEN created_at END;
```

### C-3-6: 検証プロトコル

```bash
cd backend && uv run ruff check app/ && uv run ruff format --check app/ && uv run python -m pytest tests/ -x -q
```

---

## 実施順序

```
C-1 (write-only 6列)  →  C-2 (guid)  →  C-3 (content pipeline)
      ↓ 検証                ↓ 検証            ↓ 検証 + gen-types
```

各サブステップ完了後にコミット。gen-types は C-3 完了後に一括実行。

---

## 影響ファイル一覧（全サブステップ合算）

### モデル / サービス / タスク

| ファイル | C-1 | C-2 | C-3 |
|---|---|---|---|
| models/news.py | x | x | x |
| services/news_fetcher.py | x | x | x |
| services/hacker_news.py | x | x | — |
| services/alpha_vantage.py | x | x | — |
| services/content_extractor.py | — | — | x |
| tasks/taskiq_worker.py | — | — | x |
| scripts/compare_models.py | x | — | x |

### テスト

| ファイル | C-1 | C-2 | C-3 |
|---|---|---|---|
| tests/conftest.py | — | — | — |
| tests/test_news_fetcher.py | x | x | x |
| tests/test_hacker_news.py | x | x | — |
| tests/test_alpha_vantage.py | x | x | — |
| tests/test_content_extractor.py | x | — | x |
| tests/test_dedup.py | x | — | — |
| tests/test_semantic_search.py | x | — | — |
| tests/test_ai_analyzer.py | x | — | — |
| tests/test_embedding.py | — | — | — |
| tests/test_routers/test_news.py | x | — | — |
| tests/test_routers/test_me.py | x | — | — |
| tests/test_routers/test_categories.py | x | — | — |
