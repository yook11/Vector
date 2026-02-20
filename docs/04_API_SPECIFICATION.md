# API仕様書 (Phase 1)

## ベースURL
```
開発: http://localhost:8000/api/v1
```

## 認証

JWT Bearer トークン認証。`Authorization: Bearer <accessToken>` ヘッダーで送信。

| マーク | 意味 |
|--------|------|
| 🔓 | 認証必須 |
| 🔓? | 認証任意（認証時に追加情報あり） |
| なし | 認証不要 |

---

## Auth エンドポイント

### POST /api/v1/auth/register

ユーザー登録。

**リクエスト**
```json
{
  "email": "user@example.com",
  "password": "password123",
  "displayName": "Alice"
}
```
`displayName` は任意。

**レスポンス (201)**
```json
{
  "id": 1,
  "email": "user@example.com",
  "displayName": "Alice",
  "isActive": true,
  "createdAt": "2026-02-15T00:00:00Z"
}
```
**レスポンス (409)**: `{ "detail": "Email already registered" }`

### POST /api/v1/auth/login

ログイン。アクセストークン（60分）とリフレッシュトークン（30日）を発行。

**リクエスト**
```json
{ "email": "user@example.com", "password": "password123" }
```

**レスポンス (200)**
```json
{
  "accessToken": "eyJ...",
  "refreshToken": "9XHz...",
  "tokenType": "bearer"
}
```
**レスポンス (401)**: `{ "detail": "Invalid email or password" }`

### POST /api/v1/auth/refresh

アクセストークンのリフレッシュ。リフレッシュトークンローテーション方式（旧トークンは無効化、新トークンを発行）。

**リクエスト**
```json
{ "refreshToken": "9XHz..." }
```

**レスポンス (200)**: login と同じ `TokenResponse`
**レスポンス (401)**: `{ "detail": "Invalid or expired refresh token" }`

### POST /api/v1/auth/logout

リフレッシュトークンを無効化。

**リクエスト**
```json
{ "refreshToken": "9XHz..." }
```

**レスポンス (204)**: No Content

---

## Health エンドポイント

### GET /api/v1/health

**レスポンス (200)**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "dbConnected": true,
  "lastFetchAt": "2026-02-15T10:00:00Z"
}
```

---

## News エンドポイント

### GET /api/v1/news 🔓?

ニュース一覧取得。分析結果を含む。認証時は `isWatched` フィールドが正確に反映される。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| keywordId | int? | null | キーワードでフィルター |
| myKeywords | bool | false | 認証ユーザーのサブスクライブ済みキーワードでフィルター |
| sentiment | string? | null | positive / negative / neutral |
| minImpact | int? | null | 最低影響度スコア (1-10) |
| sortBy | string | "publishedAt" | publishedAt / impactScore |
| sortOrder | string | "desc" | asc / desc |
| page | int | 1 | ページ番号 |
| perPage | int | 20 | 件数 (max 100) |

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
      "keywords": [
        { "id": 1, "keyword": "Quantum Computing", "category": "computing" }
      ],
      "analysis": {
        "titleJa": "量子コンピューティングの新たなブレイクスルー...",
        "summaryJa": "MITの研究チームが...\n商業化への道筋が...\n投資家にとっては...",
        "sentiment": "positive",
        "impactScore": 8,
        "keyTopics": ["量子コンピューティング", "MIT", "超電導"],
        "reasoning": "技術的ブレイクスルーであり...",
        "aiProvider": "gemini",
        "analyzedAt": "2026-02-15T10:01:00Z"
      },
      "isWatched": false
    }
  ],
  "total": 150,
  "page": 1,
  "perPage": 20,
  "totalPages": 8
}
```

注意: `analysis` が `null` の場合あり（AI分析未完了）。

### GET /api/v1/news/{id} 🔓?

ニュース詳細取得。

**レスポンス (200)**: items[0] と同じ構造
**レスポンス (404)**: `{ "detail": "News article not found" }`

### POST /api/v1/news/fetch

手動フェッチトリガー。RSSからニュースを同期的に取得する（AI分析は含まない）。

> ⚠️ 現在は認証不要。Phase 2 で認証必須に変更予定。
> ⚠️ ステータスコード 202 だが、処理は同期的に完了する。

**リクエスト (optional)**
```json
{ "keywordIds": [1, 3] }
```
空の場合は全アクティブキーワードで取得。

**レスポンス (202)**
```json
{
  "message": "Fetch completed: 10 new, 5 skipped",
  "keywordsCount": 5,
  "jobId": "fetch-20260215-100000"
}
```

---

## Keywords エンドポイント

### GET /api/v1/keywords 🔓

キーワード一覧取得。各キーワードに紐づく記事数を含む。

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "keyword": "Quantum Computing",
      "category": "computing",
      "isActive": true,
      "articleCount": 42,
      "createdAt": "2026-02-01T00:00:00Z"
    }
  ]
}
```

### POST /api/v1/keywords 🔓

**リクエスト**
```json
{ "keyword": "Material Informatics", "category": "materials" }
```

**レスポンス (201)**: 作成されたキーワード
**レスポンス (409)**: `{ "detail": "Keyword already exists" }`

### PATCH /api/v1/keywords/{id} 🔓

**リクエスト**
```json
{ "isActive": false }
```

**レスポンス (200)**: 更新後のキーワード
**レスポンス (404)**: `{ "detail": "Keyword not found" }`

### DELETE /api/v1/keywords/{id} 🔓

**レスポンス (204)**: No Content
**レスポンス (404)**: `{ "detail": "Keyword not found" }`

---

## Me エンドポイント（ユーザー固有操作）

### GET /api/v1/me/subscriptions 🔓

ユーザーのキーワードサブスクリプション一覧。

**レスポンス (200)**
```json
{
  "items": [
    {
      "id": 1,
      "keywordId": 1,
      "keyword": "Quantum Computing",
      "category": "computing",
      "createdAt": "2026-02-01T00:00:00Z"
    }
  ]
}
```

### POST /api/v1/me/subscriptions 🔓

キーワードをサブスクライブ。

**リクエスト**
```json
{ "keywordId": 1 }
```

**レスポンス (201)**: 作成されたサブスクリプション
**レスポンス (404)**: `{ "detail": "Keyword not found" }`
**レスポンス (409)**: `{ "detail": "Already subscribed" }`

### DELETE /api/v1/me/subscriptions/{keyword_id} 🔓

キーワードのサブスクリプション解除。

**レスポンス (204)**: No Content
**レスポンス (404)**: `{ "detail": "Subscription not found" }`

### GET /api/v1/me/watchlist 🔓

ユーザーのウォッチリスト一覧（ページネーション付き）。
レスポンスは軽量設計で、AI分析結果は含まない。詳細が必要な場合は `GET /api/v1/news/{id}` を使用。

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

### POST /api/v1/me/watchlist 🔓

記事をウォッチリストに追加。

**リクエスト**
```json
{ "newsArticleId": 42 }
```

**レスポンス (201)**: 作成されたウォッチリストアイテム
**レスポンス (404)**: `{ "detail": "News article not found" }`
**レスポンス (409)**: `{ "detail": "Already in watchlist" }`

### DELETE /api/v1/me/watchlist/{news_article_id} 🔓

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
| 401 | 認証エラー（未認証・トークン無効） |
| 404 | リソースが見つからない |
| 409 | 重複 |
| 500 | サーバー内部エラー |
| 503 | 外部API障害 |
