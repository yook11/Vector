# ArticleService の WatchlistRepository 依存について

## 背景

`get_watched_ids` を `ArticleRepository` から `WatchlistRepository` に移動した結果、`ArticleService` が `WatchlistRepository` を DI で受け取る構造になった。`SemanticSearchService` も同様。

```
ArticleService(repo: ArticleRepository, watchlist_repo: WatchlistRepository)
SemanticSearchService(search_repo: SemanticSearchRepository, watchlist_repo: WatchlistRepository)
```

用途は `get_watched_ids` のみ。記事レスポンスの `is_watched: bool` フラグ付与に使用。

## 違和感の正体

「データの所有」と「データの使用文脈」が一致していない。

- `get_watched_ids` は `watchlist_entries` テーブルへのクエリ → データの所有は WatchlistRepository
- 使用文脈は「記事の読み取り装飾」 → ArticleService のユースケース

ArticleService が別ドメインの Repository を直接持つことで、ドメイン境界が曖昧になる。

## 検討した選択肢

### 案1: 現状維持 (Service が両方の Repository を持つ)

Service の責務を「ユースケース全体のコーディネーター」と捉えれば、複数 Repository への依存は自然。ただし `is_watched` の付与が ArticleService の本質的責務かは疑問が残る。「記事を返す」と「記事にユーザー固有の装飾を付ける」は別の関心事という見方もできる。

### 案3: Repository 層で JOIN

ArticleRepository 内で `watchlist_entries` を JOIN し、1クエリで `is_watched` を解決する。Service の依存は ArticleRepository 1つに戻る。

**問題:**
- `user_id` が Repository 層に漏れる。現状 ArticleRepository はユーザーコンテキストを知らない設計で、これを崩すことになる
- 戻り値の型変更 (`NewsArticle` → タプル等) の影響範囲が広い

### 案5: Read-only Protocol

```python
class WatchlistReader(Protocol):
    async def get_watched_ids(self, user_id: int) -> set[int]: ...
```

ArticleService は `WatchlistReader` 経由でアクセスし、書き込みメソッドは型レベルで隠蔽される (ISP)。

**問題:**
- `get_watched_ids` 1メソッドのために Protocol を切るのは、抽象化コストに対してリターンが小さい
- WatchlistRepository に書き込みメソッドが増えて権限過剰を実感してからでも遅くない

## 判断

**ウォッチリスト層のレビュー (Phase 2) 時に再評価する。** 理由:

1. WatchlistRepository / WatchlistService 自体が未レビューで、判断材料が不足
2. ウォッチリスト層の構造次第で `get_watched_ids` の最適な置き場所が変わる可能性がある
3. 今確定しても二度手間になるリスクが高い
