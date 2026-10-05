# 分析済み記事の API 表現

Status: Implemented
工程: backend(schemas/articles.py ほか) → /gen-types → frontend → /check

## 目的

API 上の記事を「分析済み記事」という概念の名前で表し、一覧用に切り詰めた表現と区別する。
エージェントが扱う外部検索の結果 (`ExternalUrlSource` など) と取り違えない名前にする。

## 分析済み記事の範囲

Vector が収集し、対象範囲内と判定して分析を終えた記事。公開 id は `/news/{id}` の id (analyzed_articles.id)。

| 区分 | 情報 | API に出すか |
|---|---|---|
| 元記事への参照 | ニュースソース、元記事のタイトルと URL、公開日時 | 出す |
| 分析結果 | 翻訳タイトル、要約、要点、投資家視点、カテゴリ、分析日時 | 出す |
| 内部データ | 要点の mentions、埋め込みベクトル | 出さない (トレンドと検索に使う) |

次は分析済み記事に含めない。

- 元記事の本文: 元記事 (AnalyzableArticle) の情報で、分析の入力にだけ使う。
- ウォッチ状態: 利用者ごとの情報。`GET /api/v1/me/watchlist/ids` で別に返す。
- 工程の途中の id (curation / analyzable の id)。
- 外部検索の結果: 分析も保存もせず、公開 id がない。

## 型

| 型 | 中身 | 使う場所 |
|---|---|---|
| `AnalyzedArticle` | 公開する情報すべて (`id`, `translatedTitle`, `summary`, `investorTake`, `keyPoints` 全件, `analyzedAt`, `category`, `source`, `publishedAt`, `original`) | `GET /api/v1/articles/{id}` |
| `AnalyzedArticlePreview` | 一覧用に切り詰めた一部 (`id`, `translatedTitle`, `keyPoints` 先頭3件・各250字, `summaryPreview`, `category`, `source`, `publishedAt`) | `GET /api/v1/articles` と `GET /api/v1/me/watchlist` の `items`、`GET /api/v1/articles/{id}/similar` |

組み立て関数は `build_analyzed_article` / `build_analyzed_article_preview`。

## 名前の決め方

- 概念の名前は、公開する情報をすべて持つ表現に付ける (`Category` と同じ)。切り詰めた表現に付けると全部入りに読め、全件が要る場面で使われたときに要点が黙って欠ける。
- 一覧の表現は `Preview` で修飾する。既存の `summaryPreview` と同じ語で、詳細へ誘導するための一部という役割を表す。
- 採らない名前: `Brief` (量の語で概念を表さない)、`Summary` (要約フィールド `summary` と紛らわしい)、`Card` (画面部品の名前)、`ListItem` / `Minimal` (構造だけの名前)。
- 修飾のない `Article` は使わない。エージェントには外部検索の結果もあるため、アプリ内の記事が分析済みであることを名前で示す。
- [briefing-schema-naming.md](../insights/briefing-schema-naming.md) の原則2 (記事表現は Brief / Detail / Embed の三語彙に従う) は、記事についてはこの spec で置き換える。他のレスポンスに埋め込む型の `Embed` は変えない。

## 不変条件

- JSON の形、パス、operation 名 (`listArticles` / `getArticle` / `getSimilarArticles` / ウォッチリスト系) は変えない。変わるのは OpenAPI の schema 名と生成型名だけ。

## 対象外

- 一覧のページング。カーソル方式への変更と一覧レスポンス `AnalyzedArticlePreviewList` の導入は別 PR で行う。
- `PaginatedArticleResponse` / `ArticleListParams` / `ArticleService` / `ArticleRepository` (`article_eager_options_brief` / `article_eager_options_detail` を含む) の改名。
- `OriginalArticleEmbed` / `NewsSourceEmbed` / `_BriefingArticleEmbed`、`BriefingDetail` などブリーフィング側の名前。
- フロントのコンポーネント名 (`NewsDetail` / `ArticleList` / `ArticleCard`)。
- 過去の spec と docs/design-journey の記述。

## 受け入れ条件

- [x] `ArticleBrief` / `ArticleDetail` / `build_brief` / `build_detail` が backend/app・backend/tests・frontend/src と生成型から消えている
- [x] origin/main と比べた OpenAPI の差分が schema 名と説明文だけ
- [x] `/check` が pass
