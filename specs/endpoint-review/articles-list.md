# GET /api/v1/articles

## 目的・要件

ログイン後ダッシュボードの記事一覧表示。分析済みニュース記事を条件絞り込み・ソート・ページネーション付きで返す。主要ユースケース:

- **カテゴリ/キーワード絞り込み** (サイドバー)
- **ソース絞り込み** (フィルタ)
- **インパクト絞り込み** (重要度で絞る)
- **自由文検索** (`q`): Gemini Embedding で意味的検索
- **ソート** (公開日時 / インパクト)
- **認証時は watchlist 状態を返す** (★マークのハイライト)

未認証でもアクセス可能（`get_optional_user`）。

## 現状の実装

- Router: [backend/app/routers/articles.py:27-34](backend/app/routers/articles.py#L27-L34)
- Schema: [backend/app/schemas/articles.py:38-56](backend/app/schemas/articles.py#L38-L56) (`ArticleListParams`)
- Service: [backend/app/services/articles.py:77-97](backend/app/services/articles.py#L77-L97) (`list_articles`)
- Repository: [backend/app/repositories/articles.py:49-122](backend/app/repositories/articles.py#L49-L122) (`fetch_analyzed_list`)
- Tests: [backend/tests/test_routers/test_articles.py:57-261](backend/tests/test_routers/test_articles.py#L57-L261)

### 実装要点

- `Annotated[ArticleListParams, Query()]` で VO 型クエリパラメータを受け、Router → Service → Repository まで同じ型で流す
- フィルタは `WHERE id IN (subquery)` 方式で組み立てる（source, keyword, category）
- `impact_level` は `CASE` 式で順序付けし `>=` で「**以上**」の絞り込み
- ページング前に `select(func.count()).select_from(stmt.subquery())` で total を取得
- Eager load: `article_analysis`, `news_source`, `article_keywords → keyword → category` (selectinload)
- Watchlist 状態はログイン時のみ `get_watched_ids(user_id)` で全エントリを取得して set で判定

## 観点別レビュー

### 1. 目的・要件

カバレッジは十分。コアユースケース（一覧・絞り込み・検索・ソート・watchlist）はすべて実装されている。

### 2. API 設計

- URL: `GET /api/v1/articles` — 妥当
- メソッド: GET — 妥当
- ステータスコード: 200 (正常), 422 (VO バリデーション失敗) — 妥当
- クエリパラメータの camelCase alias (`perPage`, `sortBy`, `impactLevel`) — フロントの型生成と整合している
- **認証の依存注入のスタイル不整合**: クエリは `Annotated[Model, Query()]` だが認証は `Depends(get_optional_user)` の旧スタイル。`Annotated[CurrentUser | None, Depends(get_optional_user)]` に統一したい（後続エンドポイントでも共通）

### 3. スキーマ設計

`ArticleBrief` は必要最小限のカード用フィールドに絞れている。`NewsSourceEmbed` / `KeywordEmbed` から internal ID を抜く方針は [feedback_schema_design.md](../../.claude/projects/-Users-you-Vector/memory/feedback_schema_design.md) と整合。

**ただし `ArticleBrief.id: int` は internal DB ID をそのまま露出している。** これは「internal ID を露出しない」原則と矛盾する。ただし記事には URL 以外に自然な外部識別子がなく、watchlist エントリのキーや詳細ページルーティングで `id` を使うため、現実解として許容されている状態。レビュー対象の論点として記録する。

### 4. クエリ効率

| # | 問題 | 場所 |
|---|---|---|
| F1 | **Keyword.category を eager load しているが、Brief/Detail レスポンスに category は含まれない** | [repositories/articles.py:41](backend/app/repositories/articles.py#L41) |
| F2 | **watchlist は全エントリをロード**（表示対象の ID だけで良い） | [repositories/articles.py:173-179](backend/app/repositories/articles.py#L173-L179) |
| F3 | count 用に `stmt.subquery()` をラップ — eager load オプションは除外されるので実害はないが、JOIN を1回余分にコンパイルする | [repositories/articles.py:94](backend/app/repositories/articles.py#L94) |

F1 は明確に無駄。`article_keywords.selectinload(keyword)` までで止めれば 1 クエリ減る。

F2 は個人ユーザーの watchlist が大きい場合に問題になる。`WatchlistEntry.news_article_id.in_([表示対象IDs])` で絞る方が理にかなう。ただし単一ラウンドトリップなのでまずは動作優先、実際にパフォーマンス問題になってから対処でも可。

### 5. ロジック効率

| # | 問題 | 場所 |
|---|---|---|
| F4 | **`NewsSourceEmbed(id=..., name=...)` と書いているが `NewsSourceEmbed` に `id` フィールドはない** — Pydantic v2 のデフォルト `extra="ignore"` で `id` 引数は黙って捨てられている。死コード | [services/articles.py:37-39](backend/app/services/articles.py#L37-L39), [services/articles.py:58-61](backend/app/services/articles.py#L58-L61) |
| F9 | **`fetch_analyzed_list` が通常一覧とセマンティック検索を1メソッドで処理している** — 根本的に異なるデータ取得パターンが同一メソッドに混在。詳細は下記 | [repositories/articles.py:35-51](backend/app/repositories/articles.py#L35-L51) |

F4 はスキーマから `id` を抜いた時に Service 層の呼び出し側が取り残されたもの。挙動は正しい（黙って捨てられる）が、読んだ人を混乱させる。

#### F9: 通常一覧とセマンティック検索の混在（構造的問題）

**問題の本質**: 通常の記事一覧とセマンティック検索は、データの取り方自体が異なる操作である。

| | 通常一覧 | セマンティック検索 |
|---|---|---|
| 入口 | DB から条件に合う記事を取得 | クエリをベクトル化 → 全記事とベクトル比較 → 閾値で足切り |
| フィルタ | source / keyword / category / impact_level | 上記 + cosine distance < threshold |
| ソート | 常に `published_at` | `sort_by` に応じて distance or `published_at` |
| `sort_by` の意味 | **無意味**（RELEVANCE を指定しても distance がない） | 有意味（DATE or RELEVANCE を選択） |

にもかかわらず、現在の `fetch_analyzed_list` は `query_embedding: list[float] | None` で分岐し、1メソッドで両方を処理している。

**構造的な兆候**:

1. `_build_filtered_query` が `tuple[Select, ColumnElement[float] | None]` を返す — `distance_expr` の Optional が `_apply_sort` まで伝搬する
2. `_apply_sort` が `distance_expr is not None and sort_by == RELEVANCE` で条件分岐 — None チェックのリレー
3. Service 層は `query.q is not None` で embedding を取得するかどうかを判断しているのに、その判断結果を Repository に `Optional` として渡し、Repository 側でもう一度同じ分岐をしている

**対処方針**: エンドポイント・Service・Repository・Schema の全層で分離する（詳細は「決定事項: エンドポイント分離」を参照）

### 6. 異常系・境界条件

| # | 問題 | 深刻度 |
|---|---|---|
| F5 | **`q` 付きで `sortBy=publishedAt&sortOrder=desc` を明示指定しても、それがデフォルト値と一致するため distance ソートに**「静かに」**上書きされる**。クライアントが「意味検索しつつ新着順」を要求する手段がない | [repositories/articles.py:98-104](backend/app/repositories/articles.py#L98-L104) |
| F6 | **`keyword` と `category` を同時指定すると `category` が静かに無視される** — `if/elif` の分岐。エラーも警告もない | [repositories/articles.py:74-87](backend/app/repositories/articles.py#L74-L87) |
| F7 | **`impact_level` は「以上」の意味** — `impactLevel=medium` は medium + high + critical を返す。パラメータ名から「ちょうどこのレベル」を期待する可能性が高い | [repositories/articles.py:89-91](backend/app/repositories/articles.py#L89-L91) |
| F8 | `result.unique()` を呼んでいるが、`ArticleAnalysis.news_article_id` が UNIQUE なら不要のはず — 要確認 | [repositories/articles.py:121](backend/app/repositories/articles.py#L121) |

### テストカバレッジのギャップ

| # | 足りないテスト |
|---|---|
| T1 | `page` が `total_pages` を超えた場合（空 items, total は正しい） |
| T2 | `q` + 明示ソート (F5 の検証) |
| T3 | 複合フィルタ (source + impactLevel など) |
| T4 | `keyword` + `category` 同時指定 (F6 の検証) |
| T5 | `impactLevel` の ">=" 挙動 (F7 の現状動作) |
| T6 | 認証済みで `isWatched=true` が返る |
| T7 | カテゴリフィルタの正常系 (slug で絞り込み) |

## 論点と対処方針

| # | 論点 | 深刻度 | 対処方針 |
|---|---|---|---|
| F9 | `fetch_analyzed_list` が通常一覧とセマンティック検索を混在処理 | **blocker** | 全層分離: エンドポイント (`/articles` + `/articles/search`) / Service (`ArticleService` + `ArticleSearchService`) / Repository (`fetch_articles` + `search_articles`) / Schema (`ArticleListParams` + `ArticleSearchParams`)。詳細は「決定事項: エンドポイント分離」 |
| F5 | q + 明示ソートが静かに distance ソートに上書きされる | **resolved** | F9 のエンドポイント分離で解消。検索エンドポイントでは `q` 必須・`sort_by` が有効。一覧エンドポイントには `q` も `sort_by` も存在しない |
| F4 | `NewsSourceEmbed(id=...)` 死コード | should-fix | `id=...` を削除（2箇所） |
| F1 | `Keyword.category` の無駄な eager load | should-fix | `.selectinload(Keyword.category)` を削除 |
| F6 | keyword + category 同時指定で category 無視 | discuss | (A) 両方 AND で適用 / (B) 422 で拒否 / (C) 現状維持 + ドキュメント化。フロント側のサイドバー実装を見ると keyword が選ばれたら category も同じものが自動でセットされる運用なので、現状維持が無難か |
| F7 | impact_level の ">=" 挙動 | **resolved** | (B) 完全一致に変更。`_IMPACT_LEVEL_ORDER` dict は削除 |
| F2 | watchlist 全件ロード | nice-to-have | 当面保留。watchlist が実際に大きくなったら対処 |
| F3 | count 用 subquery | nice-to-have | 現状維持（可読性優先） |
| F8 | `result.unique()` の要否 | discuss | モデルの制約を確認 → 不要なら削除、必要なら理由をコメントで残す |
| S1 | `ArticleBrief.id` の露出 | discuss | 自然な外部識別子が無い以上、当面 int id を使う方針を明文化する |
| R1 | 認証依存注入のスタイル統一 | nice-to-have | `Annotated[CurrentUser \| None, Depends(get_optional_user)]` に揃える |
| T1-T7 | テスト追加 | should-fix | F5/F6 のテストは修正とセットで書く。その他はまとめて |

## 決定事項

### 決定1: impact_level を完全一致に変更

旧: `>=`（"以上"）→ 新: `==`（完全一致）。`_IMPACT_LEVEL_ORDER` dict と `_impact_order_expr` CASE 式は削除。

### 決定2: エンドポイント分離 — 記事一覧とセマンティック検索

#### 背景と判断根拠

通常の記事一覧とセマンティック検索は**根本的に異なる操作**である（F9 参照）。さらに:

- セマンティック検索は将来、LLM 推論による因果関係ベースの検索に発展させることが確定している（[semantic-search-evolution.md](../semantic-search-evolution.md)）
- `sort_by=relevance` は検索時にのみ意味があり、一覧では無意味（F5）
- 1つのエンドポイントに `q` の有無で分岐するロジックを詰め込むと、Router・Service・Repository の全層で Optional の伝搬と条件分岐が発生する

**判断: 別の操作なら、全層で分ける。**

#### エンドポイント設計

| Method | Path | 用途 | Service |
|---|---|---|---|
| GET | `/api/v1/articles` | 記事一覧（フィルタ + 日付ソート） | `ArticleService` |
| GET | `/api/v1/articles/search` | セマンティック検索（`q` 必須） | `ArticleSearchService` |
| GET | `/api/v1/articles/{id}` | 記事詳細 | `ArticleService` |
| GET | `/api/v1/articles/{id}/similar` | 類似記事 | `ArticleService` |

**`similar` は `ArticleService` に残す。** セマンティック検索（ユーザーのテキスト入力 → ベクトル化 → 探索）と類似記事（既存記事の embedding → ナビゲーション）は、cosine distance を使う点は同じだが**ドメイン上の操作が異なる**:

| | セマンティック検索 | 類似記事 |
|---|---|---|
| 入力 | ユーザーが入力したテキスト | 既存の記事 |
| ベクトルの出所 | テキストをその場で embed | 記事の既存 embedding |
| 操作の意味 | **探索** — 「このトピックの記事を探す」 | **ナビゲーション** — 「この記事に近い記事を見る」 |

#### 全層の対応関係

```
Router                    Service                    Repository
─────────────────────     ───────────────────────     ─────────────────────
articles.py               ArticleService              ArticleRepository
  GET /articles             list_articles()              fetch_articles()
  GET /articles/{id}        get_article()                fetch_one_analyzed()
  GET /articles/{id}/similar get_similar()               fetch_similar()

article_search.py         ArticleSearchService        ArticleRepository (共有)
  GET /articles/search      search()                     search_articles()
```

Repository は1ファイルのまま。メソッド名で一覧（`fetch_articles`）と検索（`search_articles`）を区別する。共通処理（`_base_query`, `_apply_content_filters`, `_apply_pagination`, `_count`）はプライベートメソッドで共有。

#### Schema 分離

```python
# 一覧用: q なし、sort_by なし
class ArticleListParams(PaginationParams):
    keyword: Annotated[KeywordName | None, Query()] = None
    category: Annotated[CategorySlug | None, Query()] = None
    source: Annotated[SourceName | None, Query()] = None
    impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC

# 検索用: q 必須、sort_by あり
class ArticleSearchParams(PaginationParams):
    q: Annotated[str, Query(min_length=1, max_length=500)]
    sort_by: Annotated[SortBy, Query(alias="sortBy")] = SortBy.RELEVANCE
    keyword: Annotated[KeywordName | None, Query()] = None
    category: Annotated[CategorySlug | None, Query()] = None
    source: Annotated[SourceName | None, Query()] = None
    impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC
```

型レベルで保証されること:
- 一覧に `q` は存在しない → セマンティック検索のコードパスに入りようがない
- 検索で `q` は必須 → `None` チェック不要
- `sort_by` は検索専用 → 一覧で `relevance` が指定される問題が消滅（F5 解消）
- `SortBy` のデフォルトは `RELEVANCE`（検索なら関連度順が自然）

`SortBy` enum は維持:

```python
class SortBy(StrEnum):
    DATE = "date"
    RELEVANCE = "relevance"
```

#### ソート規則

| エンドポイント | 条件 | ソート |
|---|---|---|
| `/articles` | — | `published_at {sort_order}, id DESC` |
| `/articles/search` | `sort_by=relevance` | `cosine_distance ASC, published_at DESC, id DESC` |
| `/articles/search` | `sort_by=date` | `published_at {sort_order}, id DESC` |

全ケースで `id DESC` を最終タイブレーカーに入れる（offset ページネーションの決定性保証）。

### セマンティック検索の将来構想

詳細は [semantic-search-evolution.md](../semantic-search-evolution.md) に記録。

現状の embedding（`original_title + original_content`）はトピック類似度しか捉えられない。因果関係ベースの検索への発展が確定しており、`ArticleSearchService` への集約はその準備でもある。段階的な改善パス:
1. `reasoning` を embedding 対象に含める（低コスト、因果的な記述を活用）
2. エンティティグラフ（企業・政策・技術の関係構造）
3. LLM re-ranking（候補を絞ってから LLM で因果判定）

### 他レイヤーへの波及（実装時に対応が必要）

- **フロントエンド**: `GET /api/v1/articles?q=...` → `GET /api/v1/articles/search?q=...` に変更（**破壊的変更**）。`/gen-types` で型再生成
- **バックエンドテスト**: 検索エンドポイントのテストを新規作成（`embed_search_query` を mock）。一覧テストから `q` 関連を除去。`impact_level` 完全一致挙動のテスト追加
- **articles-detail / articles-similar のレビュー時**: `similar` は `ArticleService` に残す方針を前提にレビューする
