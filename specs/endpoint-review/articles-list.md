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

これはスキーマから `id` を抜いた時に Service 層の呼び出し側が取り残されたもの。挙動は正しい（黙って捨てられる）が、読んだ人を混乱させる。

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
| F4 | `NewsSourceEmbed(id=...)` 死コード | should-fix | `id=...` を削除（2箇所） |
| F1 | `Keyword.category` の無駄な eager load | should-fix | `.selectinload(Keyword.category)` を削除 |
| F5 | q + 明示ソートが静かに distance ソートに上書きされる | **resolved** | `q` を純粋フィルタ化し、ソートは常に `published_at` に固定。`sort_by` パラメータごと削除。詳細は「決定事項」 |
| F6 | keyword + category 同時指定で category 無視 | discuss | (A) 両方 AND で適用 / (B) 422 で拒否 / (C) 現状維持 + ドキュメント化。フロント側のサイドバー実装を見ると keyword が選ばれたら category も同じものが自動でセットされる運用なので、現状維持が無難か |
| F7 | impact_level の ">=" 挙動 | **resolved** | (B) 完全一致に変更。`_IMPACT_LEVEL_ORDER` dict は削除 |
| F2 | watchlist 全件ロード | nice-to-have | 当面保留。watchlist が実際に大きくなったら対処 |
| F3 | count 用 subquery | nice-to-have | 現状維持（可読性優先） |
| F8 | `result.unique()` の要否 | discuss | モデルの制約を確認 → 不要なら削除、必要なら理由をコメントで残す |
| S1 | `ArticleBrief.id` の露出 | discuss | 自然な外部識別子が無い以上、当面 int id を使う方針を明文化する |
| R1 | 認証依存注入のスタイル統一 | nice-to-have | `Annotated[CurrentUser \| None, Depends(get_optional_user)]` に揃える |
| T1-T7 | テスト追加 | should-fix | F5/F6 のテストは修正とセットで書く。その他はまとめて |

## 決定事項

### 設計方針: フィルタとソートの役割を明確に分離

**フィルタ（対象集合を絞る）** — すべて同じ役割として扱う

| パラメータ | 挙動 |
|---|---|
| `category` | カテゴリ slug で絞る |
| `keyword` | キーワード名で絞る |
| `source` | ソース名で絞る |
| `impact_level` | **完全一致** で絞る (旧: `>=` "以上") |
| `q` | 意味的類似度がしきい値以下のものだけ残す。**純粋にフィルタ。並び順には一切関与しない** |

**ソート（並び順を決める）** — 選択肢は1軸のみ

- 常に `NewsArticle.published_at`
- `sort_order` で新しい順 / 古い順を切り替え
- デフォルト: 新しい順 (`DESC`)

### なぜ `q` をフィルタとして扱うか

`q` が効く場面で「類似度順」と「日付順」のどちらも正当なユースケース（例: 「AI チップの最新ニュース」）だが、ニュースプラットフォームの性質上、**関連性の担保はしきい値で十分** で、**「このトピックの新しい記事を見たい」が主要ユースケース**。

ソートモード切り替え（relevance vs publishedAt）を導入すると API が複雑化する割に、ユーザーが意識するメンタルモデルは「キーワードで絞って、新着順で見る」の一つで十分。シンプルさを優先。

### 具体的な変更（[backend/app/repositories/articles.py](backend/app/repositories/articles.py) 中心）

1. **import 整理**: `from sqlmodel import func, select` → `from sqlalchemy import case, func, select` に統合
2. **impact_level を完全一致に**: `_impact_order_expr >= min_order` → `ArticleAnalysis.impact_level == query.impact_level`
3. **`sort_by` パラメータ削除**: `ArticleSortField` enum ごと廃止。`ArticleListParams` から `sort_by` フィールド削除
4. **セマンティック検索のソート分岐削除**: `is_default_sort` ローカル変数と `if query_embedding is not None and is_default_sort` 分岐を削除。ソート節は1行に圧縮

### SortBy の再導入（キャッシュ設計と同時に確定）

前回の決定で `sort_by` を廃止したが、セマンティック検索の位置づけを再整理した結果、`SortBy` を導入する。

**背景**: セマンティック検索は「高機能な LIKE 検索」であり、トピック類似度フィルタにすぎない。将来的には LLM 推論による因果関係ベースの検索に発展させる構想がある（[semantic-search-evolution.md](../semantic-search-evolution.md)）。現段階ではフィルタとソートの役割分離を維持しつつ、関連度ソートの選択肢を提供する。

```python
class SortBy(StrEnum):
    DATE = "date"
    RELEVANCE = "relevance"
```

| 条件 | ソート |
|---|---|
| q なし（sort_by 問わず） | `published_at {sort_order}, id DESC` |
| q あり + `sort_by=date` | `published_at {sort_order}, id DESC` |
| q あり + `sort_by=relevance` | `cosine_distance ASC, published_at DESC, id DESC`（`sort_order` 無視） |

- `sort_by` のデフォルトは `DATE`
- q なしで `sort_by=relevance` を指定しても 422 にせず、黙って日付順にフォールバック
- **全ケースで `id DESC` を最終タイブレーカーに入れる** — バッチフェッチで `published_at` が同秒の記事が複数存在しうるため、offset ページネーションの決定性を保証する

### 派生的に削除されるもの

| 対象 | 場所 | 理由 |
|---|---|---|
| `_IMPACT_LEVEL_ORDER` dict | [repositories/articles.py:26-31](backend/app/repositories/articles.py#L26-L31) | `>=` 比較がなくなり参照されなくなる |
| `_impact_order_expr` CASE 式 | [repositories/articles.py:18-24](backend/app/repositories/articles.py#L18-L24) | `sort_by=impactLevel` 廃止で参照元が消える |
| `ArticleSortField` enum | [schemas/articles.py:23-25](backend/app/schemas/articles.py#L23-L25) | `SortBy(DATE, RELEVANCE)` に置き換え |
| `is_default_sort` ローカル変数 | [repositories/articles.py:98-101](backend/app/repositories/articles.py#L98-L101) | ソート分岐を書き直す |

### 最終的なソートコード

```python
# q あり + RELEVANCE の場合
if query_embedding is not None and query.sort_by == SortBy.RELEVANCE:
    distance_expr = ArticleAnalysis.embedding.cosine_distance(query_embedding)
    stmt = stmt.order_by(distance_expr.asc(), NewsArticle.published_at.desc(), NewsArticle.id.desc())
else:
    order = NewsArticle.published_at.desc() if query.sort_order == SortOrder.DESC else NewsArticle.published_at.asc()
    stmt = stmt.order_by(order, NewsArticle.id.desc())
```

### 最終的な `ArticleListParams`

```python
class ArticleListParams(PaginationParams):
    keyword: Annotated[KeywordName | None, Query()] = None
    category: Annotated[CategorySlug | None, Query()] = None
    source: Annotated[SourceName | None, Query()] = None
    impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None
    q: Annotated[str | None, Query(min_length=1, max_length=500)] = None
    sort_by: Annotated[SortBy, Query(alias="sortBy")] = SortBy.DATE
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC
```

### セマンティック検索の将来構想

詳細は [specs/semantic-search-evolution.md](../semantic-search-evolution.md) に記録。

現状の embedding（`original_title + original_content`）はトピック類似度しか捉えられない。段階的な改善パスとして:
1. `reasoning` を embedding 対象に含める（低コスト、因果的な記述を活用）
2. エンティティグラフ（企業・政策・技術の関係構造）
3. LLM re-ranking（候補を絞ってから LLM で因果判定）

### 他レイヤーへの波及（実装時に対応が必要）

- **フロントエンド**: `sortBy` パラメータの値を `publishedAt` / `impactLevel` から `date` / `relevance` に変更。`/gen-types` で型再生成
- **バックエンドテスト**: `impact_level` 完全一致挙動のテストを1本追加（medium 指定で high/critical が返らないことを保証）。`sort_by=relevance` + `q` の組み合わせテスト追加
- **articles-detail / articles-similar のレビュー時**: `_impact_order_expr` / `ArticleSortField` が他のエンドポイントから参照されていないことを確認する（現状は article list のみで使用されているはず）
