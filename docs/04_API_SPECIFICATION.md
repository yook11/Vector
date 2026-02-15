# API仕様書 (Phase 1)

## ベースURL
```
開発: http://localhost:8000/api/v1
```

## エンドポイント一覧

### GET /api/v1/health

**レスポンス (200)**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "dbConnected": true,
  "lastFetchAt": "2025-02-15T10:00:00Z"
}
```

### GET /api/v1/news

ニュース一覧取得。分析結果を含む。

**クエリパラメータ**

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| keywordId | int? | null | キーワードでフィルター |
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
      "publishedAt": "2025-02-15T08:00:00Z",
      "fetchedAt": "2025-02-15T10:00:00Z",
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
        "analyzedAt": "2025-02-15T10:01:00Z"
      }
    }
  ],
  "total": 150,
  "page": 1,
  "perPage": 20,
  "totalPages": 8
}
```

注意: `analysis` が `null` の場合あり（AI分析未完了）。

### GET /api/v1/news/{id}

ニュース詳細取得。

**レスポンス (200)**: items[0] と同じ構造
**レスポンス (404)**: `{ "detail": "News article not found" }`

### POST /api/v1/news/fetch

手動フェッチトリガー。

**リクエスト (optional)**
```json
{ "keywordIds": [1, 3] }
```
空の場合は全アクティブキーワードで取得。

**レスポンス (202)**
```json
{
  "message": "Fetch started",
  "keywordsCount": 5,
  "jobId": "fetch-20250215-100000"
}
```

### GET /api/v1/keywords

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
      "createdAt": "2025-02-01T00:00:00Z"
    }
  ]
}
```

### POST /api/v1/keywords

**リクエスト**
```json
{ "keyword": "Material Informatics", "category": "materials" }
```

**レスポンス (201)**: 作成されたキーワード
**レスポンス (409)**: `{ "detail": "Keyword already exists" }`

### PATCH /api/v1/keywords/{id}

**リクエスト**
```json
{ "isActive": false }
```

**レスポンス (200)**: 更新後のキーワード
**レスポンス (404)**: `{ "detail": "Keyword not found" }`

### DELETE /api/v1/keywords/{id}

**レスポンス (204)**: No Content
**レスポンス (404)**: `{ "detail": "Keyword not found" }`

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
| 404 | リソースが見つからない |
| 409 | 重複 |
| 500 | サーバー内部エラー |
| 503 | 外部API障害 |
