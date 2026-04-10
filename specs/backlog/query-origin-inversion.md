# クエリ起点の反転: NewsArticle → ArticleAnalysis

## 前提

`refactor/article-identity` で外部ID統一 + WatchlistEntry FK張替えは完了済み。
外部識別子は `article_analyses.id` に統一されたが、**コード上のクエリ起点は `NewsArticle` のまま**。

## 問題

ドメインの主体は `ArticleAnalysis`（ユーザーが見る・操作する対象）だが、Repository 層の全クエリが `select(NewsArticle)` から始まっている。

### 現状のデータフロー

```
Repository: select(NewsArticle).join(ArticleAnalysis, ...)
    ↓ NewsArticle を返す
Service: build_brief(article: NewsArticle)
    ↓ article.article_analysis.xxx でアクセス
Schema: ArticleBrief / ArticleDetail
```

### あるべき姿

```
Repository: select(ArticleAnalysis).join(NewsArticle, ...)
    ↓ ArticleAnalysis を返す
Service: build_brief(analysis: ArticleAnalysis)
    ↓ analysis.news_article.xxx でアクセス（source, published_at, keywords, original）
Schema: 変更なし
```

## NewsArticle から使っているフィールド

| フィールド | 用途 | 使用箇所 |
|---|---|---|
| `news_source.name` | ソース名表示 | brief + detail |
| `published_at` | 公開日表示 | brief + detail |
| `article_keywords` | キーワードタグ | brief + detail |
| `original_title` | 原文タイトル | detail のみ |
| `original_url` | 原文リンク | detail のみ |
| `original_content` | 原文本文 | detail のみ |

## 修正順序

### Step 1: eager_options を ArticleAnalysis 起点に書き換え

`article_eager_options()` を `ArticleAnalysis` 起点に変更。
`news_article.news_source`, `news_article.article_keywords` をチェーンで eager load。
他の層に影響しないので単独で変更・検証できる。

### Step 2: Repository + Service を同時に変更（メソッド単位）

返り値型が変わるため、Repository と Service はセットで変更する:

1. `fetch_articles` + `list_articles` / `build_brief`
2. `fetch_one_analyzed` + `get_article` / `build_detail`
3. `fetch_similar_to` + `get_similar`
4. `fetch_watched_articles`（watchlist repo + service）

### Step 3: テスト修正

Step 2 の各メソッドで型が変わるので、対応するテストも合わせて直す。
