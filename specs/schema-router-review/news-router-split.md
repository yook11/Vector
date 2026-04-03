# News ルーター分割 — ユーザー向け / パイプライン操作の責務分離

## 現状の問題

`routers/news.py` に性質の異なる2種類のエンドポイントが同居している。

| エンドポイント | 性質 | 認証 | 消費者 |
|---|---|---|---|
| `GET /api/v1/news` | ユーザー向け参照 | optional user | フロントエンド |
| `GET /api/v1/news/{id}` | ユーザー向け参照 | optional user | フロントエンド |
| `GET /api/v1/news/{id}/similar` | ユーザー向け参照 | なし | フロントエンド |
| `POST /api/v1/news/fetch` | パイプライン操作 | admin | cron / 管理画面 |
| `POST /api/v1/news/embed` | パイプライン操作 | admin | cron / 管理画面 |

### なぜ問題か

1. **変更理由の違い（SRP 違反）** — 一覧フィルタの追加とパイプライン処理の変更は独立した関心事。一方を変更するとき、無関係なもう一方のコードが視界に入る
2. **認証ポリシーの違い** — ユーザー向けは `optional_user` / 公開、パイプラインは `admin` 必須。同一ルーターに混在することで、認証の付け忘れリスクが高まる
3. **運用上の分離困難** — パイプライン系にレート制限やタイムアウトを個別設定したい場合、ルーターが分かれていなければ middleware の適用が煩雑になる
4. **リソース名の不一致** — ユーザーにとっては「分析済みの記事を読む」行為であり、`news` は内部実装の語彙（ニュースソースから取得したもの）が API に漏れている

## 解決策: 2ルーターへの分割とリネーム

### Before

```
routers/news.py     → /api/v1/news/*（5エンドポイント混在）
```

### After

```
routers/articles.py  → /api/v1/articles/*（ユーザー向け参照）
routers/pipeline.py  → /api/v1/pipeline/*（管理者向けパイプライン操作）
```

### articles.py — 分析済み記事を読む

| メソッド | パス | 処理内容 |
|---|---|---|
| GET | `/api/v1/articles` | 分析済み記事一覧（フィルタ/ソート/ページング/セマンティック検索） |
| GET | `/api/v1/articles/{id}` | 記事詳細（原文+分析+関連情報） |
| GET | `/api/v1/articles/{id}/similar` | pgvector cosine距離で類似記事を返す |

- 認証: `optional_user`（ウォッチリスト状態の判定用）または公開
- 消費者: フロントエンド
- Service/Repository: 既存の `NewsService` / `NewsRepository` をそのまま利用（内部名はリネーム不要）

### pipeline.py — パイプラインを操作する

| メソッド | パス | 処理内容 |
|---|---|---|
| POST | `/api/v1/pipeline/fetch` | ニュース取得タスクをキューに投入 |
| POST | `/api/v1/pipeline/embed` | embedding 未生成の分析にベクトルを一括付与 |

- 認証: `admin` 必須
- 消費者: cron / 管理画面
- 将来拡張: 再分析、キーワード抽出など他のパイプライン操作が追加されても `/api/v1/pipeline/` 配下に自然に収まる

### リネームの根拠

| 変更 | 理由 |
|---|---|
| `/news` → `/articles` | ユーザーが消費するのは「分析済み記事」であり、データの出自（news source からの取得物）ではない。消費者の視点に合わせた命名 |
| `/news/fetch`, `/news/embed` → `/pipeline/fetch`, `/pipeline/embed` | パイプライン操作は記事リソースの CRUD ではない。操作の性質に合わせた命名。特定リソースに紐づかないため拡張にも開いている |

## 波及先

| ファイル | 変更内容 |
|---|---|
| `backend/app/routers/news.py` | 削除 |
| `backend/app/routers/articles.py` | 新規作成（GET 3エンドポイント） |
| `backend/app/routers/pipeline.py` | 新規作成（POST 2エンドポイント） |
| `backend/app/main.py` | `include_router` を `news.router` から `articles.router` + `pipeline.router` に変更 |
| `frontend/src/lib/api-client.ts` | `/news` → `/articles` にパス変更 |
| `frontend/src/lib/client-api.ts` | `/news/fetch` → `/pipeline/fetch` にパス変更 |
| `frontend/src/types/generated.ts` | `/gen-types` 再実行で自動更新 |
| `backend/tests/test_routers/test_news.py` | テストファイル分割またはリネーム |

## 設計判断

### 内部の Service / Repository 名はリネームしない

`NewsService`, `NewsRepository` はドメインモデル（`NewsArticle`）に対応した命名であり、API のリソース名とは独立した関心事。API の消費者向け命名（`articles`）と内部のドメイン命名（`news`）は別のレイヤーの判断であるため、今回は変更しない。

### URL パス構造はフラット（`/pipeline/fetch`）

現時点で pipeline 操作は news 固有の `fetch` と `embed` の2つのみ。将来的に別リソースのパイプライン操作が増えた場合に `/pipeline/news/fetch` のようなネストを検討するが、現時点ではフラットで十分。
