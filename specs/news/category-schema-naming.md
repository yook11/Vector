# カテゴリの API 表現

Status: Implemented
工程: backend(schemas/category.py ほか) → /gen-types → frontend → /check

## 目的

カテゴリを API 上で1つの型 `Category` で表し、カテゴリについての集計値を `CategoryStats` に分ける。
値を1つ足すたびにカテゴリが別の名前になる状態をなくす。

## 型

| 型 | 中身 | 使う場所 |
|---|---|---|
| `Category` | `slug`, `name` | 記事 (`ArticleBrief` / `ArticleDetail`)、ブリーフィング (`BriefingDetail` / `EmptyBriefing` / `BriefingListItem`)、`CategoryStats` |
| `CategoryStats` | `category: Category`, `articleCount24h: int` | `GET /api/v1/categories` の `items` |
| `CategoryStatsList` | `items: CategoryStats[]` | `GET /api/v1/categories` |

- `articleCount24h` は、`analyzed_at` が直近24時間以内の分析済み記事の数。記事の公開日時ではない。全カテゴリについて返し、0 も省略しない。
- `Category` は `id` を持たない (表示と絞り込みに不要)。

## 名前の決め方

- 型名は中身ではなく概念で決める。`CategoryWithArticleCount` のように中身を並べた名前は、値を足すたびに増える。`CategoryListItem` のように構造だけを表す名前は、何を入れてよいかの境界がない。
- カテゴリ自体の属性 (色・説明など) は `Category` に足す。記事カードやブリーフィングにも同時に入る。
- カテゴリについての集計値 (別期間の件数・最新記事の時刻など) は `CategoryStats` にフィールドとして足す。型名は増やさない。
- 集計値ではない値は `CategoryStats` に入れない。別の画面が必要とするなら、その画面のレスポンスで扱う。

## 不変条件

- 記事・ブリーフィングの `category` の JSON 形 (`{slug, name}`) は変えない。
- `GET /api/v1/categories` のパス・BFF 経由証明・並び順 (slug 順) は変えない。

## 破壊的変更

- `GET /api/v1/categories` の要素の形: `{slug, name, recentCount}` → `{category: {slug, name}, articleCount24h}`。frontend と同じ PR で反映し、rollout 中の数分の不整合は許容する。
- 生成型名: `CategoryEmbed` → `Category`、`CategoryDetail(List)` → `CategoryStats(List)`。

## 対象外

- `NewsSourceEmbed` / `OriginalArticleEmbed` の改名。
- trend_discovery の `_CategoryTrends` のカテゴリ表現。
- エンドポイントのパスと operation 名。

## 受け入れ条件

- [x] `CategoryEmbed` / `CategoryDetail` / `recentCount` がコードと生成物から消えている
- [x] 生成型で `articleCount24h` が必須 (`number`) になっている
- [x] frontend に件数の undefined 分岐がない
- [x] 未使用の `CategorySidebar` / `MobileSidebar` / `useBuildSearchParamsHref` を削除した
- [x] `/check` が pass
