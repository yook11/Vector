# ルーター層 — 使用状況レビュー

## 背景

3層分離（Repository/Service/Router）が全5ルーターで完了した。
責務が分離されたことで、各エンドポイントが「何の処理をしているか」が明確になった。
ここから逆算して、処理 → 必要なデータ → クエリ の対応を整理し、不要なエンドポイントを特定する。

## 全エンドポイント一覧と使用状況

### categories（1 エンドポイント）

| メソッド | パス | 処理内容 | FE 使用 |
|---|---|---|---|
| GET | `/categories` | カテゴリ一覧 + キーワード + 記事数を集約して返す | Dashboard サイドバー |

**問題なし**。単一エンドポイントで、Dashboard の中核機能。

### keywords（4 エンドポイント）

| メソッド | パス | 処理内容 | FE 使用 |
|---|---|---|---|
| GET | `/keywords` | キーワード一覧 + カテゴリ + 記事数 | **未使用** |
| POST | `/keywords` | キーワード新規作成 | AddKeywordDialog |
| PATCH | `/keywords/{id}` | キーワードのカテゴリ変更 | **未使用** |
| DELETE | `/keywords/{id}` | キーワード削除 | KeywordRow |

#### 問題: GET と PATCH が未使用

- **GET `/keywords`**: `GET /categories` がカテゴリ+キーワード+記事数を一括返却するため、キーワード単独の一覧は不要。フロントエンドに呼び出し箇所がない
- **PATCH `/keywords/{id}`**: カテゴリ変更UIが存在しない。Service 層に `update_keyword()` が実装済みだが、呼び出し元がない

### news（5 エンドポイント）

| メソッド | パス | 処理内容 | FE 使用 |
|---|---|---|---|
| GET | `/news` | 分析済み記事一覧（フィルタ/ソート/ページング/セマンティック検索） | Dashboard 一覧 |
| GET | `/news/{id}` | 記事詳細（原文+分析+関連情報） | 詳細ページ |
| GET | `/news/{id}/similar` | pgvector cosine距離で類似記事を返す | 詳細ページ |
| POST | `/news/embed` | embedding未生成の分析にベクトルを一括付与 | **未使用** |
| POST | `/news/fetch` | ニュース取得タスクをキューに投入 | FetchButton (Admin) |

#### 問題: POST `/news/embed` が未使用

- フロントエンドにUIなし、バッチジョブやcronからの呼び出しもなし
- Service 層に `embed_news()` が実装済みで、`embed_articles()` を呼び出す
- **用途の想定**: 分析パイプラインで embedding 生成が失敗した記事を手動で backfill する管理API
- **判断が必要**: 運用で手動実行する場面があるなら残す。パイプライン側で自動リトライするなら不要

### news_sources（5 エンドポイント）

| メソッド | パス | 処理内容 | FE 使用 |
|---|---|---|---|
| GET | `/sources` | ソース全件取得 | Dashboard フィルタ + Settings 管理画面 |
| GET | `/sources/{id}` | ソース単体取得 | **未使用** |
| POST | `/sources` | ソース新規作成 | SourceFormDialog |
| DELETE | `/sources/{id}` | ソース削除 | SourceTable |
| PATCH | `/sources/{id}/toggle` | is_active の切り替え | SourceTable |

#### 問題: GET `/sources/{id}` が未使用

- 一覧 API で全件取得しており、個別取得の需要がない
- Router に `_get_or_404()` ヘルパーがあり、`get_source`, `delete_source`, `toggle_source` で共有されている
- `get_source` エンドポイントを削除しても、`_get_or_404()` は他のエンドポイントが使うため残る
- Service 層の `get_source()` メソッドは `model_validate()` するだけで、ほぼ無意味

### me / watchlist（3 エンドポイント）

| メソッド | パス | 処理内容 | FE 使用 |
|---|---|---|---|
| GET | `/me/watchlist` | ユーザーのウォッチリスト一覧（NewsBrief 形式） | Watchlist ページ |
| POST | `/me/watchlist` | 記事をウォッチリストに追加 | WatchlistButton |
| DELETE | `/me/watchlist/{id}` | 記事をウォッチリストから削除 | WatchlistButton |

**問題なし**。全エンドポイントがフロントエンドから使用されている。

## 未使用エンドポイントまとめ

| エンドポイント | 関連する実装 | 削除影響 |
|---|---|---|
| GET `/keywords` | `KeywordService.list_keywords()`, `KeywordRepository.fetch_all_with_stats()` | Service/Repo のメソッドも未使用になる |
| PATCH `/keywords/{id}` | `KeywordService.update_keyword()`, `KeywordRepository.get_by_id()`, `save()`, `category_exists()` | `get_by_id()` は delete でも使用。`update_keyword()` のみ削除対象 |
| POST `/news/embed` | `NewsService.embed_news()`, `NewsRepository.get_analyses_without_embedding()`, `embed_articles()` | embedding サービスへの依存を含む。判断保留 |
| GET `/sources/{id}` | `NewsSourceService.get_source()`, `NewsSourceRepository.get_by_id()` | `get_by_id()` は delete/toggle でも使用。`get_source()` のみ削除対象 |

## news_sources ルーターの構造的問題

### _get_or_404 がルーターに存在している

```python
# routers/news_sources.py
async def _get_or_404(repo: NewsSourceRepository, source_id: int):
    source = await repo.get_by_id(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="News source not found")
    return source
```

3層分離の方針では、Router は Repository に直接触らない。
しかし `_get_or_404` は Router 層で Repository を直接呼び出している。

他のルーター（keywords, watchlist）では、Service 層で `NotFoundError` を raise し、Router が `HTTPException` に変換するパターンが確立されている。news_sources だけが古いパターンのまま。

### delete / toggle で Service が ORM オブジェクトを受け取っている

```python
# Router 側で ORM を取得して Service に渡す
source = await _get_or_404(repo, source_id)
await service.delete_source(source)
```

他のルーターでは `service.delete_keyword(keyword_id)` のように ID を渡し、存在チェックは Service 内部で行う。news_sources は Router が存在チェックの責務を持ってしまっている。

## keywords ルーターで GET 一覧が不要な理由

### データフローの重複

```
GET /categories → CategoryService.list_categories()
  → repo.fetch_categories()      # カテゴリ一覧
  → repo.fetch_keyword_stats()   # キーワード + 記事数（カテゴリ別）
  → repo.fetch_category_article_counts()

GET /keywords → KeywordService.list_keywords()
  → repo.fetch_all_with_stats()  # キーワード + カテゴリ + 記事数
```

categories API が返す `CategoryDetail.keywords: list[KeywordStatEmbed]` に、keyword_id / name / article_count が含まれている。フロントエンドのサイドバーはこのデータを使っているため、keywords の GET 一覧は冗長。

## 処理 → データ → クエリの逆引き（使用中エンドポイント）

### Dashboard 一覧（GET `/news`）

```
処理: 分析済み記事をフィルタ/ソート/ページングして返す
  ↓
データ: NewsArticle + ArticleAnalysis(INNER JOIN) + NewsSource + Keywords + WatchlistEntry
  ↓
クエリ:
  1. fetch_analyzed_list() — INNER JOIN article_analysis, フィルタ/ソート/LIMIT/OFFSET
  2. get_watched_ids()    — user_id で watchlist_entry を検索（ログイン時のみ）
  3. (検索時) embed_search_query() — クエリ文字列をベクトル化、cosine距離でソート
```

### 記事詳細（GET `/news/{id}`）

```
処理: 単一記事の全情報を返す
  ↓
データ: NewsArticle + ArticleAnalysis + NewsSource + Keywords + WatchlistEntry + 原文
  ↓
クエリ:
  1. fetch_one_analyzed() — id で 1 件取得、eager load
  2. get_watched_ids()    — ログイン時のみ
```

### 類似記事（GET `/news/{id}/similar`）

```
処理: 対象記事の embedding に近い記事を返す
  ↓
データ: ArticleAnalysis.embedding + NewsArticle + ArticleAnalysis + NewsSource + Keywords
  ↓
クエリ:
  1. get_analysis()   — 対象記事の embedding を取得
  2. fetch_similar()  — pgvector cosine_distance で ORDER BY、LIMIT
```

### カテゴリ一覧（GET `/categories`）

```
処理: カテゴリ + 配下キーワード + 記事数を集約
  ↓
データ: Category + Keyword + article_keywords(COUNT)
  ↓
クエリ:
  1. fetch_categories()           — カテゴリ一覧
  2. fetch_keyword_stats()        — キーワード別記事数（DISTINCT article_id の COUNT）
  3. fetch_category_article_counts() — カテゴリ別記事数
```

### ウォッチリスト一覧（GET `/me/watchlist`）

```
処理: ユーザーがブックマークした分析済み記事を返す
  ↓
データ: WatchlistEntry + NewsArticle + ArticleAnalysis + NewsSource + Keywords
  ↓
クエリ:
  1. fetch_watched_articles() — watchlist_entry JOIN news_article INNER JOIN article_analysis
```

### ソース管理（GET/POST/DELETE/TOGGLE `/sources`）

```
処理: ニュースソースの CRUD + 有効/無効切替
  ↓
データ: NewsSource
  ↓
クエリ:
  GET:    get_all() — 全件 ORDER BY name
  POST:   create()  — INSERT + RETURNING
  DELETE: get_by_id() + delete()
  TOGGLE: get_by_id() + save() — is_active を反転
```

### キーワード管理（POST/DELETE `/keywords`）

```
処理: キーワードの作成/削除
  ↓
データ: Keyword + Category（参照チェック）
  ↓
クエリ:
  POST:   get_by_name() + category_exists() + create() + fetch_one_with_stats()
  DELETE: get_by_id() + delete()
```

## 推奨アクション

### 削除対象（即実行可能）

1. **GET `/keywords`** — エンドポイント + `KeywordService.list_keywords()` + `KeywordRepository.fetch_all_with_stats()`
2. **PATCH `/keywords/{id}`** — エンドポイント + `KeywordService.update_keyword()` + `KeywordUpdate` スキーマ
3. **GET `/sources/{id}`** — エンドポイント + `NewsSourceService.get_source()`

### 要判断

4. **POST `/news/embed`** — 管理用バッチAPIとして残すか、パイプラインの自動リトライに統合するか

### 構造修正（削除と同時に実施）

5. **news_sources ルーターの `_get_or_404` 廃止** — delete/toggle を ID ベースに変更し、Service 層で NotFoundError を raise するパターンに統一
