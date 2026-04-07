# 記事識別子とFK方向の再設計

## 問題の本質

Vector において価値を生んでいるのは `ArticleAnalysis`（AI分析済み記事）であり、`NewsArticle`（取得した生記事）ではない。ユーザーが画面で見る・操作する対象は全て分析済み記事。しかし現在のシステムは `news_article.id` を外部識別子として使っており、ドメインの実態と乖離している。

## 現状の問題

### 1. WatchlistEntry の FK が生記事を指している

```
WatchlistEntry.news_article_id → news_articles.id
```

ユーザーがウォッチしているのは「分析済み記事」なのに、FK は「生記事」を指している。DB レベルで「未分析記事のウォッチ」を防げない。アプリケーション不変条件（画面に出るのは分析済みのみ）に依存しており、3層防御（Pydantic / VO / DB制約）の原則に反する。

### 2. 外部識別子が news_article.id になっている

- `ArticleBrief.id` → `news_article.id`
- `GET /api/v1/articles/{id}` → `news_article.id`
- `WatchlistCreate.news_id` → `news_article.id`
- `DELETE /api/v1/me/watchlist/{news_id}` → `news_article.id`

システム全体の外部識別子が生記事の PK になっている。FK だけ `article_analyses.id` に変えると、外部識別子との不整合が発生し、全ての操作で ID 変換が必要になる。

### 3. FK変更と外部識別子はセットで解決すべき

FK だけ変えると中途半端な状態になる:
- API は `news_article.id` で受け取る
- DB は `article_analyses.id` で保存する
- 境界ごとに変換が必要

## 設計方針

### テーブル分離は維持する

`NewsArticle` と `ArticleAnalysis` は概念が異なる:

- **NewsArticle**: fetcher が外部から取得した生データ。scraping パイプラインの成果物
- **ArticleAnalysis**: analyzer が AI で生成した知的成果物。分析パイプラインの成果物

1:1 の関係であっても統合しない。工程・関心事・ライフサイクルが異なる。

### 外部識別子を article_analyses.id に統一する

ユーザーが操作する対象は分析済み記事なので、外部識別子もそれに合わせる:

- `ArticleBrief.id` → `article_analyses.id`
- `GET /api/v1/articles/{id}` → `article_analyses.id`
- `WatchlistCreate` → `article_analyses.id` ベース
- `WatchlistEntry` FK → `article_analyses.id`

### 再分析ポリシー

- `ArticleAnalysis` 行は UPDATE-in-place。DELETE + INSERT による入れ替えはしない
- 過去記事の再分析は運用上想定しない
- この方針により `article_analyses.id` は安定した識別子として機能する

## 影響範囲

| レイヤー | 影響 |
|---|---|
| DB | `watchlist_entries` FK 変更 + データ移行。Alembic マイグレーション |
| Models | `WatchlistEntry.news_article_id` → `article_analysis_id` |
| Schemas | `ArticleBrief.id` / `ArticleDetail.id` の値ソース変更 |
| Routers | パスパラメータ・リクエストボディの意味が変わる |
| Services | `build_brief` / `build_detail` の id 参照元変更 |
| Repositories | 全クエリの JOIN パス変更 |
| Frontend | 型再生成。ID の意味が変わるためキャッシュ等に影響ありうる |

## 事前確認事項

- `article_analyses.news_article_id` の UNIQUE 制約の有無（1:1 保証）
- `watchlist_entries` に孤児レコード（対応する `article_analyses` がない）が存在しないか
- カスケード削除: `news_articles` → `article_analyses` → `watchlist_entries` の2段カスケード設計
