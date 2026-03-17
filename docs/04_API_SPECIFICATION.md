# API仕様書

## ベースURL
```
開発: http://localhost:8000/api/v1 (Docker internal のみ — BFF プロキシ経由)
```

## 認証

Better Auth + BFF プロキシ構成。ブラウザから FastAPI への直接アクセスは不可。

### 認証フロー

1. ブラウザ → Next.js BFF `/api/auth/*` で Better Auth セッション管理 (Cookie)
2. ブラウザ → Next.js BFF `/api/proxy/*` でバックエンドへのリクエストをプロキシ
3. BFF がセッション検証後、内部ヘッダーを付与して FastAPI に転送

### 内部ヘッダー

| ヘッダー | 説明 |
|---------|------|
| `X-Internal-Secret` | BFF→Backend 信頼検証用シークレット |
| `X-User-ID` | Better Auth user.id (cuid, VARCHAR(32)) |
| `X-User-Role` | ユーザーロール ("user" / "admin") |

### 認証マーク

| マーク | 意味 |
|--------|------|
| AUTH | 認証必須 (get_current_user) |
| AUTH? | 認証任意 (get_optional_user) |
| ADMIN | admin ロール必須 (get_admin_user) |
| なし | 認証不要 |

---

## Health エンドポイント

### GET /api/v1/health

**レスポンス (200)**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "dbConnected": true
}
```

---

## News エンドポイント

### GET /api/v1/news [AUTH?]

ニュース一覧取得。分析結果を含む。認証時は `isWatched` フィールドが正確に反映される。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| keywordId | int? | null | キーワードでフィルター |
| kwCategoryId | int? | null | キーワードカテゴリでフィルター |
| sourceId | int? | null | ニュースソースでフィルター |
| myKeywords | bool | false | 認証ユーザーのサブスクライブ済みキーワードでフィルター |
| sentiment | string? | null | positive / negative / neutral |
| minImpact | int? | null | 最低影響度スコア (1-10) |
| category | string? | null | 投資カテゴリスラッグでフィルター |
| deduplicated | bool | true | 重複記事を非表示（canonical のみ表示） |
| q | string? | null | セマンティック検索クエリ (1-500文字) |
| sortBy | string | "publishedAt" | publishedAt / impactScore |
| sortOrder | string | "desc" | asc / desc |
| page | int | 1 | ページ番号 |
| perPage | int | 12 | 件数 (max 100) |
| locale | string | "ja" | 翻訳言語コード (ja / en) |

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "titleOriginal": "New Quantum Computing Breakthrough...",
      "url": "https://...",
      "source": "Google News",
      "publishedAt": "2026-02-15T08:00:00Z",
      "fetchedAt": "2026-02-15T10:00:00Z",
      "content": "Full article text...",
      "contentFetchedAt": "2026-02-15T10:00:30Z",
      "keywords": [
        {
          "id": 1,
          "keyword": "Quantum Computing",
          "categories": [{ "slug": "quantum", "name": "量子" }]
        }
      ],
      "analysis": {
        "title": "量子コンピューティングの新たなブレイクスルー...",
        "summary": "MITの研究チームが...",
        "sentiment": "positive",
        "impactScore": 8,
        "reasoning": "技術的ブレイクスルーであり...",
        "aiModel": { "id": 1, "provider": "gemini", "name": "gemini-2.5-flash-lite" },
        "analyzedAt": "2026-02-15T10:01:00Z",
        "investmentCategories": [
          { "slug": "growth_catalyst", "name": "成長期待" }
        ]
      },
      "isWatched": false,
      "duplicateCount": 2,
      "articleGroupId": 5
    }
  ],
  "total": 150,
  "page": 1,
  "perPage": 12,
  "totalPages": 13
}
```

注意:
- `analysis` が `null` の場合あり（AI分析未完了）
- `content` が `null` の場合あり（全文未取得）
- `duplicateCount` は同一グループ内の他記事数（0 = 重複なし）

### GET /api/v1/news/{id} [AUTH?]

ニュース詳細取得。

**レスポンス (200)**: items[0] と同じ構造
**レスポンス (404)**: `{ "detail": "News article not found" }`

### POST /api/v1/news/embed [ADMIN]

embeddingが未生成の記事に対してベクトル埋め込みをバックフィル生成する。

**リクエスト**: なし

**レスポンス (200)**
```json
{
  "message": "Embedding completed: 10 embedded, 0 errors",
  "embeddedCount": 10,
  "skippedCount": 0,
  "errorCount": 0
}
```

### GET /api/v1/news/{id}/similar

指定記事に類似する記事を pgvector のコサイン距離で検索。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| limit | int | 5 | 返却件数 (1-20) |
| locale | string | "ja" | 翻訳言語コード |

**レスポンス (200)**: `NewsResponse[]`（配列）
**レスポンス (404)**: `{ "detail": "News article not found" }`

注意: 対象記事の embedding が未生成の場合は空配列 `[]` を返す（404ではない）。

### GET /api/v1/news/groups/{group_id} [AUTH?]

重複記事グループに属する全記事を取得。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| locale | string | "ja" | 翻訳言語コード |

**レスポンス (200)**: `NewsResponse[]`（配列）
**レスポンス (404)**: `{ "detail": "Article group not found" }`

### POST /api/v1/news/fetch [ADMIN]

手動フェッチトリガー。taskiq にタスクを投入し、タスク ID を即座に返す。

**リクエスト (optional)**
```json
{ "sourceIds": [1, 3] }
```
空の場合は全アクティブソースで取得。

**レスポンス (202)**
```json
{
  "message": "Fetch task submitted",
  "sourcesCount": 2,
  "jobId": "abc123-task-id"
}
```

---

## Keywords エンドポイント

### GET /api/v1/keywords [AUTH]

キーワード一覧取得。各キーワードに紐づく記事数を含む。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| locale | string | "ja" | 翻訳言語コード |

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "keyword": "Quantum Computing",
      "categories": [{ "slug": "quantum", "name": "量子" }],
      "articleCount": 42,
      "createdAt": "2026-02-01T00:00:00Z"
    }
  ]
}
```

### POST /api/v1/keywords [ADMIN]

**リクエスト**
```json
{ "keyword": "Material Informatics", "categoryIds": [5] }
```

**レスポンス (201)**: 作成されたキーワード
**レスポンス (409)**: `{ "detail": "Keyword already exists" }`

### PATCH /api/v1/keywords/{id} [ADMIN]

**リクエスト**
```json
{ "categoryIds": [3, 5] }
```

**レスポンス (200)**: 更新後のキーワード
**レスポンス (404)**: `{ "detail": "Keyword not found" }`

### DELETE /api/v1/keywords/{id} [ADMIN]

**レスポンス (204)**: No Content
**レスポンス (404)**: `{ "detail": "Keyword not found" }`

---

## Sources エンドポイント

### GET /api/v1/sources [AUTH]

ニュースソース一覧取得。

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "name": "TechCrunch",
      "sourceType": "rss",
      "siteUrl": "https://techcrunch.com",
      "isActive": true,
      "feedUrl": "https://techcrunch.com/feed/",
      "apiEndpoint": null,
      "fetchIntervalMinutes": 720,
      "nextFetchAt": "2026-02-15T16:00:00Z",
      "lastFetchedAt": "2026-02-15T04:00:00Z",
      "consecutiveErrors": 0,
      "lastErrorMessage": null,
      "createdAt": "2026-02-01T00:00:00Z",
      "updatedAt": "2026-02-15T04:00:00Z"
    }
  ],
  "total": 7
}
```

### GET /api/v1/sources/{id} [AUTH]

ニュースソース詳細取得。

**レスポンス (200)**: items[0] と同じ構造
**レスポンス (404)**: `{ "detail": "News source not found" }`

### POST /api/v1/sources [ADMIN]

**リクエスト**
```json
{
  "name": "New Feed",
  "sourceType": "rss",
  "siteUrl": "https://example.com",
  "feedUrl": "https://example.com/feed/",
  "fetchIntervalMinutes": 360
}
```

**レスポンス (201)**: 作成されたソース
**レスポンス (400)**: `{ "detail": "feed_url is required for RSS sources" }`

### PUT /api/v1/sources/{id} [ADMIN]

ソース更新。

**レスポンス (200)**: 更新後のソース
**レスポンス (404)**: `{ "detail": "News source not found" }`

### PATCH /api/v1/sources/{id}/toggle [ADMIN]

ソースの有効/無効をトグル。再有効化時は `nextFetchAt` をリセット。

**レスポンス (200)**: 更新後のソース
**レスポンス (404)**: `{ "detail": "News source not found" }`

### DELETE /api/v1/sources/{id} [ADMIN]

**レスポンス (204)**: No Content
**レスポンス (404)**: `{ "detail": "News source not found" }`

---

## Categories エンドポイント

### GET /api/v1/categories

投資カテゴリ一覧取得（翻訳済み）。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| locale | string | "ja" | 翻訳言語コード |

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "slug": "growth_catalyst",
      "name": "成長期待",
      "description": "事業成長を加速させる可能性がある..."
    }
  ]
}
```

---

## Keyword Categories エンドポイント

### GET /api/v1/keyword-categories

キーワードカテゴリ一覧取得。各カテゴリに属するキーワードと記事数を含む。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| locale | string | "ja" | 翻訳言語コード |

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "slug": "quantum",
      "name": "量子",
      "articleCount": 120,
      "keywords": [
        { "id": 1, "keyword": "Quantum Computing", "articleCount": 42 },
        { "id": 2, "keyword": "Quantum Sensing", "articleCount": 15 }
      ]
    }
  ]
}
```

---

## Me エンドポイント（ユーザー固有操作）

### GET /api/v1/me/subscriptions [AUTH]

ユーザーのキーワードサブスクリプション一覧。

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "keywordId": 1,
      "keyword": "Quantum Computing",
      "categories": [{ "slug": "quantum", "name": "量子" }],
      "createdAt": "2026-02-01T00:00:00Z"
    }
  ]
}
```

### POST /api/v1/me/subscriptions [AUTH]

キーワードをサブスクライブ。

**リクエスト**
```json
{ "keywordId": 1 }
```

**レスポンス (201)**: 作成されたサブスクリプション
**レスポンス (404)**: `{ "detail": "Keyword not found" }`
**レスポンス (409)**: `{ "detail": "Already subscribed to this keyword" }`

### DELETE /api/v1/me/subscriptions/{keyword_id} [AUTH]

キーワードのサブスクリプション解除。

**レスポンス (204)**: No Content
**レスポンス (404)**: `{ "detail": "Subscription not found" }`

### GET /api/v1/me/watchlist [AUTH]

ユーザーのウォッチリスト一覧（ページネーション付き）。
レスポンスは軽量設計で、AI分析結果は含まない。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| page | int | 1 | ページ番号 |
| perPage | int | 20 | 件数 (max 100) |

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "newsArticleId": 42,
      "titleOriginal": "Quantum Computing Breakthrough...",
      "url": "https://...",
      "source": "Google News",
      "publishedAt": "2026-02-15T08:00:00Z",
      "createdAt": "2026-02-15T12:00:00Z"
    }
  ],
  "total": 5,
  "page": 1,
  "perPage": 20,
  "totalPages": 1
}
```

### POST /api/v1/me/watchlist [AUTH]

記事をウォッチリストに追加。

**リクエスト**
```json
{ "newsArticleId": 42 }
```

**レスポンス (201)**: 作成されたウォッチリストアイテム
**レスポンス (404)**: `{ "detail": "News article not found" }`
**レスポンス (409)**: `{ "detail": "Article already in watchlist" }`

### DELETE /api/v1/me/watchlist/{news_article_id} [AUTH]

記事をウォッチリストから削除。

**レスポンス (204)**: No Content
**レスポンス (404)**: `{ "detail": "Watchlist item not found" }`

---

## 命名規約

| レイヤー | 規約 | 例 |
|---------|------|-----|
| DB カラム (SQLModel) | snake_case | `news_article_id`, `impact_score` |
| API レスポンス (JSON) | camelCase | `newsArticleId`, `impactScore` |
| TypeScript 型 | camelCase | `newsArticleId`, `impactScore` |

Pydantic で `alias_generator = to_camel` を設定して自動変換。

## 共通エラーレスポンス

```json
{ "detail": "エラーメッセージ" }
```

| ステータス | 用途 |
|-----------|------|
| 400 | バリデーションエラー |
| 401 | 認証エラー（未認証・ヘッダー無効） |
| 403 | 権限不足（admin 必要） |
| 404 | リソースが見つからない |
| 409 | 重複 |
| 500 | サーバー内部エラー |
| 503 | 外部API障害 |
