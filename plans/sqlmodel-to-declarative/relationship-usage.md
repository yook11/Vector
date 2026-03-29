# Relationship 使用状況一覧

調査日: 2026-03-29
対象: backend/app/ 配下の全 .py ファイル

## 凡例

- **selectinload**: クエリ時の eager loading で使用
- **attribute access**: ロード済みオブジェクトからの属性アクセス
- **not used**: relationship() 定義はあるが、コード上どこからも参照されていない

---

## 1. 実際に使用されている Relationship

### NewsArticle.news_source (M:1 → NewsSource)

| ファイル | 行 | 使用パターン |
|---|---|---|
| routers/news.py | 134 | `selectinload(NewsArticle.news_source)` — _news_eager_options() |
| routers/news.py | 120 | `article.news_source.name` — _build_news_response() |
| routers/me.py | 40-42 | `selectinload(WatchlistEntry.news_article).selectinload(NewsArticle.news_source)` — list_watchlist() |
| routers/me.py | 54-60 | `item.news_article.id`, `.original_title`, `.original_url`, `.news_source.name`, `.published_at` — list_watchlist() |
| routers/me.py | 86 | `selectinload(NewsArticle.news_source)` — add_to_watchlist() |
| routers/me.py | 116 | `article.news_source.name` — add_to_watchlist() |

**使用頻度: 高（6箇所）**

### NewsArticle.article_analysis (1:1 → ArticleAnalysis)

| ファイル | 行 | 使用パターン |
|---|---|---|
| routers/news.py | 133 | `selectinload(NewsArticle.article_analysis)` — _news_eager_options() |
| routers/news.py | 105-114 | `article.article_analysis` — _build_news_response() |

**使用頻度: 中（2箇所）**

### WatchlistEntry.news_article (M:1 → NewsArticle)

| ファイル | 行 | 使用パターン |
|---|---|---|
| routers/me.py | 40 | `selectinload(WatchlistEntry.news_article)` — list_watchlist() |
| routers/me.py | 54-60 | `item.news_article.id`, `item.news_article.original_title` 等 — list_watchlist() |

**使用頻度: 中（2箇所）**

### Keyword.category (M:1 → Category)

| ファイル | 行 | 使用パターン |
|---|---|---|
| routers/keywords.py | 35 | `selectinload(Keyword.category)` — list_keywords() |
| routers/keywords.py | 48-49 | `kw.category.slug`, `kw.category.name` — list_keywords() |
| routers/keywords.py | 135 | `selectinload(Keyword.category)` — update_keyword() |
| routers/keywords.py | 146-147 | `keyword.category.slug`, `keyword.category.name` — update_keyword() |
| routers/news.py | 73 | `selectinload(ArticleKeyword.keyword).selectinload(Keyword.category)` — _load_article_keywords() |
| routers/news.py | 87-88 | `link.keyword.category.slug` — _load_article_keywords() |

**使用頻度: 高（6箇所）** ※ 既に DeclarativeBase 移行済み

### ArticleKeyword.keyword (M:1 → Keyword)

| ファイル | 行 | 使用パターン |
|---|---|---|
| routers/news.py | 73 | `selectinload(ArticleKeyword.keyword)` — _load_article_keywords() |
| routers/news.py | 81-91 | `link.keyword.id`, `link.keyword.name` 等 — _load_article_keywords() |

**使用頻度: 中（2箇所）** ※ 既に DeclarativeBase 移行済み

---

## 2. 使用されていない Relationship（定義のみ）

| Relationship | 定義ファイル | 備考 |
|---|---|---|
| **NewsSource.articles** (1:N → NewsArticle) | models/news_source.py:53 (コメントのみ) | DeclarativeBase 移行済み。cross-base のため一時削除、全モデル移行後に復元予定 |
| **NewsSource.fetch_logs** (1:N → FetchLog) | models/news_source.py:54 (コメントのみ) | 同上 |
| **FetchLog.source** (M:1 → NewsSource) | models/fetch_log.py:40 | 使用箇所なし |
| **Category.keywords** (1:N → Keyword) | models/category.py:33 | 使用箇所なし（別クエリで取得） |
| **ArticleAnalysis.news_article** (M:1 → NewsArticle) | models/article_analysis.py:63 | FK 直接参照で代替 |
| **Keyword.article_keywords** (1:N → ArticleKeyword) | models/keyword.py:55-57 | 明示的 join で代替 |
| **NewsArticle.watchlist_entries** (1:N → WatchlistEntry) | models/news_article.py:80-82 | 使用箇所なし |

---

## 3. Cross-Base 制約（現行）

ArticleKeyword (DeclarativeBase) ↔ NewsArticle (SQLModel) 間は registry 不一致のため relationship() 不可。

| ファイル | 行 | 内容 |
|---|---|---|
| models/news_article.py | 79 | `# article_keywords: cross-base — FK only` |
| models/article_keyword.py | 29 | `# news_article: cross-base — FK only, no relationship` |
| routers/news.py | 60-94 | `_load_article_keywords()` で明示的クエリにより cross-base 境界を橋渡し |

**全モデル DeclarativeBase 移行後に解消可能。**

### scripts/compare_models.py（対象外）

| ファイル | 行 | 内容 |
|---|---|---|
| scripts/compare_models.py | 181 | `selectinload(NewsArticle.analyses)` — 旧スキーマ（AnalysisResult/translations）向け比較スクリプト |

現行モデルに `NewsArticle.analyses` は存在しない。legacy コードのため移行対象外。

### 段階移行時に発生する逆方向 cross-base 問題

モデルを個別に DeclarativeBase へ移行すると、**移行先モデルを参照する側** の SQLModel relationship も cross-base で壊れる。

例: NewsSource のみ先行移行した場合:

| 壊れる relationship | 方向 | 使用状況 |
|---|---|---|
| `NewsArticle.news_source` | SQLModel → DeclarativeBase | **6 箇所で使用（selectinload + attribute access）** |
| `FetchLog.source` | SQLModel → DeclarativeBase | 未使用 |

`NewsArticle.news_source` は routers/news.py, routers/me.py で selectinload 経由で使われており、コメントアウトするとアプリが壊れる。

**結論:** 段階コミットは不可。全モデルを 1 コミットで移行する必要がある。

---

## 4. 移行時の影響分析

### 高影響（selectinload + attribute access を書き換える可能性）
- `NewsArticle.news_source` — routers/news.py, routers/me.py
- `NewsArticle.article_analysis` — routers/news.py
- `WatchlistEntry.news_article` — routers/me.py

### 低影響（定義のみ・使用箇所なし）
- `NewsSource.articles`, `NewsSource.fetch_logs`
- `FetchLog.source`
- `ArticleAnalysis.news_article`
- `NewsArticle.watchlist_entries`

### 移行不要（既に DeclarativeBase）
- `Keyword.category`, `ArticleKeyword.keyword`, `Category.keywords`, `Keyword.article_keywords`
