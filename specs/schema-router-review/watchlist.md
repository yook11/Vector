# Watchlist — スキーマ / ルーターレビュー

## 対象ファイル

| レイヤー | ファイル |
|---|---|
| Model | `backend/app/models/watchlist_entry.py` |
| Schema | `backend/app/schemas/user.py` |
| Router | `backend/app/routers/me.py` |
| Frontend | `frontend/src/app/(protected)/watchlist/page.tsx`, `frontend/src/components/news/WatchlistButton.tsx` |

## 機能の本質

Watchlist は「ユーザーが分析済みニュースをお気に入り登録し、一覧で見返す」機能。
認証トークンからユーザーを特定し、ニュースの ID だけで登録/解除する。

## スキーマ一覧（現状 — news_id リネーム済み）

| クラス | 用途 | フィールド |
|---|---|---|
| `WatchlistCreate` | `POST /me/watchlist` 入力 | `news_id` |
| `WatchlistResponse` | ウォッチリスト項目 | `news_id`, `original_title`, `original_url`, `source`, `published_at`, `created_at` |
| `WatchlistListResponse` | リストラッパー | `items`, `total`, `page`, `per_page`, `total_pages` |

## 問題点

### 1. WatchlistResponse は NewsBrief の劣化コピー（解消済み: news_article_id → news_id）

~~`news_article_id` は DB テーブル名をそのまま API に露出していた。~~
→ `news_id` にリネーム済み (bc6c096)

### 2. Watchlist 専用の出力スキーマが不要

ユーザーがブックマークするのは「分析済みニュース」。
Watchlist 一覧で表示したいのは Dashboard と同じ `NewsBrief` の情報（翻訳タイトル、サマリ、キーワード等）。

現状の `WatchlistResponse` は `NewsBrief` と別の独自フィールド構成を手作りしている:

| Dashboard (NewsBrief) | Watchlist (WatchlistResponse) | 備考 |
|---|---|---|
| `translatedTitle` | `original_title` | **主従逆転**: 翻訳タイトルを見せるべき |
| `summary` | なし | Watchlist では要約が見えない |
| `source: NewsSourceEmbed` | `source: NewsSourceEmbed` | 一致 |
| `keywords` | なし | タグが見えない |
| `isWatched` | なし | 不要（Watchlist 内なので常に true） |
| — | `original_url` | **未使用**: FE で参照されていない |
| — | `created_at` | ブックマーク日時 |

### 3. ブックマーク日時（created_at）は不要

`savedAt` 的な情報はユーザーにとって意味が薄い。
ユーザーが気にするのは「何をブックマークしたか」であって「いつブックマークしたか」ではない。
ソート順（新しいブックマークが上）で十分。

### 4. 命名規約との不一致

`WatchlistResponse` / `WatchlistListResponse` の `Response` サフィックスは他モデルでは廃止済み。
ただし専用スキーマ自体が不要になるため、リネームではなく削除が正しい対応。

### 5. ファイル名が内容と一致しない

`user.py` という名前だが中身は Watchlist スキーマのみ。

## 再設計方針

### 入力スキーマ

`WatchlistCreate` は `news_id` のみで正しい（ユーザー ID は認証トークンから取得）。据え置き。
ファイルを `watchlist.py` に移動する。

### 出力スキーマ

`WatchlistResponse` / `WatchlistListResponse` を廃止し、既存の `PaginatedNewsResponse`（`NewsBrief` のリスト）を再利用する。

```
GET /me/watchlist → PaginatedNewsResponse  # NewsBrief[] + ページネーション
```

これにより:
- Dashboard と Watchlist で同じ記事の見え方が一致する
- ニュースのデータ構造を2箇所でメンテナンスする必要がなくなる
- `NewsBrief.isWatched` は Watchlist 内では常に `true` になる

### ルーター変更

`GET /me/watchlist` のクエリを変更:
- `article_analysis` を INNER JOIN（分析済みのみ、Dashboard と同じ）
- 既存の `_build_news_brief()` ヘルパーを再利用してレスポンスを構築

`POST /me/watchlist` の戻り値:
- 現状はレスポンスを FE で破棄している（`clientAddToWatchlist` → `void`）
- 201 ステータスのみ返すか、`NewsBrief` を返すかは要検討

## 実装プラン（ルーター層リファクタと同時実行予定）

### Step 1: 共有ヘルパーの抽出

**新規:** `backend/app/routers/_news_helpers.py`

`news.py` から以下の関数を移動（`me.py` でも再利用するため）:
- `build_news_brief()` — ORM → `NewsBrief` 変換
- `build_keyword_embeds()` — ORM → `KeywordEmbed[]` 変換
- `news_eager_options()` — selectinload オプション
- `get_watched_ids()` — ユーザーの watchlist ID 取得

`news.py` は `_news_helpers.py` からインポートに切り替え。

### Step 2: スキーマファイルのリネーム + 不要スキーマ削除

- `backend/app/schemas/user.py` → `backend/app/schemas/watchlist.py` にリネーム
- `WatchlistCreate` のみ残す（`news_id: int`）
- `WatchlistResponse` / `WatchlistListResponse` を削除

### Step 3: ルーター `me.py` の更新

**GET /me/watchlist:**
- 戻り値: `PaginatedNewsResponse`
- クエリ: `WatchlistEntry` → JOIN `NewsArticle` → INNER JOIN `ArticleAnalysis`（分析済みのみ）
- eager load: `news_eager_options()` を再利用
- ビルド: `build_news_brief()` を再利用
- `is_watched`: 全アイテムが watched → `watched_ids = {全記事ID}` を渡す

**POST /me/watchlist:**
- 戻り値: 201 ボディなし（FE で破棄しているため）
- `response_model` を削除、`status_code=201` のみ
- 記事存在チェック + 重複チェックは維持

**DELETE /me/watchlist/{news_id}:**
- 変更なし

### Step 4: テスト更新 (`test_me.py`)

| テスト | 変更内容 |
|---|---|
| `test_returns_watchlist_items` | `originalTitle` → `translatedTitle`, `summary`, `isWatched=True` をアサート |
| `test_pagination` | `second_article` に `ArticleAnalysis` を追加（INNER JOIN で必要） |
| `test_add_success` | ステータス 201 のみ確認、ボディのアサーション削除 |

### Step 5: フロントエンド型更新

**`frontend/src/types/index.ts`:**
- `WatchlistResponse` / `WatchlistListResponse` の re-export を削除

**`frontend/src/lib/api-client.ts`:**
- `getWatchlist()` の戻り値型: `WatchlistListResponse` → `PaginatedNewsResponse`
- `addToWatchlist()` の戻り値型: `WatchlistResponse` → `void`

**`frontend/src/lib/client-api.ts`:** 変更なし（既に `void` を返している）

### Step 6: Watchlist ページの更新 (`watchlist/page.tsx`)

- カスタムカードレイアウトを `NewsList` コンポーネントに置換（Dashboard と同一）
- `NewsBrief` フィールドを使用（`translatedTitle`, `summary`, `keywords`）
- `formatDate` ヘルパー、`createdAt` 表示を削除
- ページネーションは既存のものを維持

### Step 7: 型生成 + 検証

1. `/gen-types` スキルで `generated.ts` 再生成
2. Backend: `ruff check` + `ruff format --check` + `pytest`
3. Frontend: `biome check` + `tsc --noEmit`

### 変更ファイル一覧

| ファイル | 操作 |
|---|---|
| `backend/app/routers/_news_helpers.py` | 新規 |
| `backend/app/routers/news.py` | ヘルパーのインポート元変更 |
| `backend/app/schemas/user.py` → `watchlist.py` | リネーム + 2スキーマ削除 |
| `backend/app/routers/me.py` | GET/POST の戻り値変更 |
| `backend/tests/test_routers/test_me.py` | アサーション更新 |
| `frontend/src/types/index.ts` | 不要 re-export 削除 |
| `frontend/src/lib/api-client.ts` | 戻り値型変更 |
| `frontend/src/app/(protected)/watchlist/page.tsx` | `NewsList` ベースに書き換え |
| `frontend/src/types/generated.ts` | 自動再生成 |
