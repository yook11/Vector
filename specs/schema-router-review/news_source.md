# NewsSource — スキーマ / ルーターレビュー

## 対象ファイル

| レイヤー | ファイル |
|---|---|
| Model | `backend/app/models/news_source.py` |
| Schema | `backend/app/schemas/news_source.py` |
| Router | `backend/app/routers/news_sources.py` |
| Frontend | `frontend/src/components/sources/SourceTable.tsx`, `frontend/src/components/sources/SourceManager.tsx`, `frontend/src/components/news/NewsFilters.tsx` |

## スキーマ一覧（現状）

| クラス | 用途 | フィールド |
|---|---|---|
| `NewsSourceCreate` | `POST /sources` の入力 | `name`, `source_type`, `site_url`, `endpoint_url` |
| `NewsSourceDetail` | 単体の全フィールド返却 | `id`, `name`, `source_type`, `site_url`, `endpoint_url`, `is_active`, `created_at`, `updated_at` |
| `NewsSourceDetailList` | リストラッパー | `items: list[NewsSourceDetail]`, `total` |

## フロントエンド使用状況

### データフロー

```
getSources() [SSR]
  ├── Dashboard page.tsx → NewsFilters (ソース絞り込みドロップダウン)
  └── Settings page.tsx  → SourceManager → SourceTable (管理テーブル)

client-api.ts [Client]
  ├── clientCreateSource()  ← SourceFormDialog
  ├── clientToggleSource()  ← SourceTable
  └── clientDeleteSource()  ← SourceTable
```

### コンポーネント別フィールド使用

| コンポーネント | 画面 | 使用フィールド |
|---|---|---|
| **NewsFilters** | Dashboard | `id`, `name` |
| **SourceTable** | Settings | `id`, `name`, `sourceType`, `endpointUrl`, `isActive` |
| **SourceFormDialog** | Settings | なし（新規作成のみ） |

## 問題点

### 1. NewsFilters に過剰な型を渡している

Dashboard の `NewsFilters` はソースの `id` と `name` だけでドロップダウンを描画する。
しかし現状では `getSources()` → `NewsSourceDetailList` を取得し、`NewsSourceDetail`（8フィールド）をそのまま渡している。

他モデルでは同様のケースに Embed 型を使っている:
- `CategoryEmbed` — slug + name（`KeywordDetail` 内で使用）
- `KeywordEmbed` — id + name（`NewsBrief` 内で使用）

NewsSource にも `NewsSourceEmbed`（id + name）があれば、
`NewsBrief.sourceName: str` を `source: NewsSourceEmbed` に置き換えて構造化でき、
NewsFilters 用の型としても再利用できる。

### 2. SourceTable で未使用のフィールドがある

`NewsSourceDetail` の `siteUrl`, `createdAt`, `updatedAt` は Settings の SourceTable でも表示していない。
管理画面で今後表示する可能性があるため Detail に残すのは妥当だが、
現時点で全エンドポイントが同じ Detail を返す設計に一覧/詳細の区別がない。

### 3. Dashboard と Settings で同じエンドポイント・同じ型を共有している

Dashboard のフィルタ用途には軽量な型（Embed）で十分なのに対し、
Settings の管理テーブルには Detail が必要。
現状は両方とも `GET /sources` → `NewsSourceDetailList` を使っており、
Dashboard 側が不必要に重い型に依存している。
