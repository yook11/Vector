# Watchlist — スキーマ / ルーターレビュー

## 対象ファイル

| レイヤー | ファイル |
|---|---|
| Model | `backend/app/models/watchlist_entry.py` |
| Schema | `backend/app/schemas/user.py` |
| Router | `backend/app/routers/me.py` |
| Frontend | `frontend/src/app/(protected)/watchlist/page.tsx`, `frontend/src/components/news/WatchlistButton.tsx` |

## スキーマ一覧（現状）

| クラス | 用途 | フィールド |
|---|---|---|
| `WatchlistCreate` | `POST /me/watchlist` 入力 | `news_article_id` |
| `WatchlistResponse` | ウォッチリスト項目 | `news_article_id`, `original_title`, `original_url`, `source`, `published_at`, `created_at` |
| `WatchlistListResponse` | リストラッパー | `items`, `total`, `page`, `per_page`, `total_pages` |

## フロントエンド使用状況

### データフロー

```
getWatchlist(page) [SSR]
  └── watchlist/page.tsx
        使用: items, page, totalPages
        未使用: total, perPage

WatchlistButton [Client]
  ├── clientAddToWatchlist(newsArticleId)    → POST（レスポンス破棄）
  └── clientRemoveFromWatchlist(newsArticleId) → DELETE
```

### フィールド使用

| フィールド | watchlist/page.tsx | 用途 |
|---|---|---|
| `newsArticleId` | 使用 | React key, リンク `/news/{id}`, WatchlistButton |
| `originalTitle` | 使用 | カードタイトル表示 |
| `source.name` | 使用 | ソース名テキスト表示 |
| `publishedAt` | 使用 | 日付表示 |
| `createdAt` | 使用 | 「Saved」日付表示 |
| `originalUrl` | **未使用** | 詳細は `/news/{id}` に遷移して見る |
| `source.id` | **未使用** | 表示のみで name しか参照されていない |

## 問題点

### 1. フィールド名が DB 構造を露出している

`news_article_id` は DB テーブル名 `news_article` の FK をそのまま API に露出している。
ユーザーが操作しているのは「ニュース」であって「news_article レコード」ではない。

- `WatchlistCreate.news_article_id` → ドメイン的には `news_id` で十分
- `WatchlistResponse.news_article_id` → 同上

### 2. レスポンスの主従が逆転している

このアプリの価値は AI 分析結果。Dashboard の `NewsBrief` では `translatedTitle`（翻訳タイトル）を表示している。
しかし Watchlist に遷移すると `originalTitle`（原文タイトル）が表示される。
同じ記事なのに見え方が変わり、一貫性がない。

ユーザーがブックマークしたのは「分析済みニュース」であって「原文記事」ではない。
レスポンスには翻訳タイトル・サマリなど AI 分析結果を主フィールドとして含めるべき。

### 3. 未使用フィールドが含まれている

- `original_url` — フロントエンドで一切表示されていない。詳細は `/news/{id}` に遷移して見る設計
- `WatchlistListResponse.total` — ページネーションに `totalPages` を使っており未参照
- `WatchlistListResponse.per_page` — API 呼び出し時に固定値 20 を渡しており未参照

### 4. 命名規約との不一致

他モデルでは `Response` サフィックスを廃止済みだが、Watchlist スキーマは旧命名のまま。

| 現在 | 規約に沿った命名 |
|---|---|
| `WatchlistResponse` | 用途に応じて再設計が必要 |
| `WatchlistListResponse` | 同上 |

### 5. ファイル名が内容と一致しない

`user.py` という名前だが中身は Watchlist スキーマのみ。`watchlist.py` の方が適切。

## 設計方針メモ

Watchlist は NewsBrief のサブセット的な見え方をユーザーに提供すべき。
「原文記事のブックマーク」ではなく「分析済みニュースのブックマーク」として再設計する。
