# ページネーション責務の漏出

## 現状の問題

### 1. `total_pages` 計算がサービス層に散在

`math.ceil(total / per_page) if total > 0 else 0` が 3 箇所にコピペされている:

- `services/articles.py:93` — `ArticleService.list_articles`
- `services/watchlist.py:30` — `WatchlistService.list_watchlist`
- `services/semantic_search.py:37` — `SemanticSearchService.search`

この計算はビジネスロジックではなく **ページネーションの定義の一部**。
仕様変更（例: 最低 1 ページ返す、0 件でも 1 を返す等）があれば全箇所修正が必要。

### 2. PaginationParams VO の境界が崩れている

`PaginationParams` を受け取りながら、サービス / リポジトリで即座にプリミティブに分解している:

```python
# サービス側: VO を分解してレスポンスに詰めている
page=pagination.page,
per_page=pagination.per_page,
total_pages=math.ceil(total / pagination.per_page) if total > 0 else 0,

# watchlist repo: VO を受け取らずバラの引数
async def fetch_watched_articles(self, user_id, page, per_page)
```

VO を作った目的（page / per_page をひとまとめに扱う）が活かされていない。

### 3. offset 計算もリポジトリ各所に散在

`(page - 1) * per_page` がリポジトリ 3 箇所で個別に計算されている:

- `repositories/articles.py:78`
- `repositories/watchlist.py:40`
- `repositories/semantic_search.py:78`

## 影響範囲

| レイヤー | ファイル | 変更内容 |
|---|---|---|
| Schema | `schemas/base.py` | `PaginationParams` に `offset` / `limit` プロパティ追加 |
| Schema | `schemas/articles.py` | `PaginatedArticleResponse.create` ファクトリ追加 |
| Service | `services/articles.py` | `PaginatedArticleResponse.create` に切り替え |
| Service | `services/watchlist.py` | 同上 |
| Service | `services/semantic_search.py` | 同上 |
| Repository | `repositories/watchlist.py` | `PaginationParams` を引数で受け取り `pagination.offset` / `pagination.limit` を使用 |
| Repository | `repositories/articles.py` | 同上（既に `ArticleListParams` 経由で VO を受け取っているので offset/limit の書き換えのみ） |
| Repository | `repositories/semantic_search.py` | 同上（既に `SemanticSearchParams` 経由） |

## 備考

- `ArticleRepository.fetch_articles` と `SemanticSearchRepository.search_articles` は既に `ArticleListParams` / `SemanticSearchParams`（`PaginationParams` のサブクラス）を VO のまま受け取っている。`WatchlistRepository.fetch_watched_articles` だけがプリミティブ引数。
- API レスポンス形式の変更はない（`PaginatedArticleResponse` のフィールドは不変）。
