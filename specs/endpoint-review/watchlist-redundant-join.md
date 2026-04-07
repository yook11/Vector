# ウォッチリスト一覧の冗長な ArticleAnalysis JOIN

## 現状の問題

`WatchlistRepository.fetch_watched_articles` で `ArticleAnalysis` を INNER JOIN している:

```python
base = (
    select(NewsArticle)
    .join(WatchlistEntry, WatchlistEntry.news_article_id == NewsArticle.id)
    .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)  # ← 不要
    .where(WatchlistEntry.user_id == user_id)
)
```

## なぜ不要か

アプリケーション不変条件として **ウォッチされた記事は必ず分析済み**:

1. ユーザーに表示される記事は全て分析済み（全一覧エンドポイントが `ArticleAnalysis` を INNER JOIN してフィルタ）
2. ウォッチ登録は画面に表示された記事に対してのみ行われる
3. よって `WatchlistEntry` が存在する `NewsArticle` には必ず `ArticleAnalysis` が存在する

この JOIN は起こり得ない状況（未分析記事のウォッチ）への防御であり、無駄なクエリコストを発生させている。

## 影響箇所

- `repositories/watchlist.py` — `fetch_watched_articles` メソッド

## 備考

- `ArticleAnalysis` のデータ自体は `article_eager_options()` の `selectinload` で取得済みなので、JOIN を外しても `article.article_analysis` へのアクセスに影響はない
- カウントクエリ (`count_stmt`) にも不要な JOIN が含まれており、両方から除去できる
