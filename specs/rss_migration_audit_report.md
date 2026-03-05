# フィード購読型移行 — 現状調査レポート

## 1. 記事取得パイプライン

### Google News RSS の URL 構築方法

[news_fetcher.py:21-23](backend/app/services/news_fetcher.py#L21-L23) でテンプレートを定義:

```python
GOOGLE_NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q={keyword}&hl=en-US&gl=US&ceid=US:en"
)
```

[news_fetcher.py:40-42](backend/app/services/news_fetcher.py#L40-L42) で `urllib.parse.quote` を使いキーワードを URL エンコードして埋め込む。言語・地域は `en-US` 固定。

### 取得フロー（5フェーズパイプライン）

起点: [taskiq_worker.py:134](backend/app/tasks/taskiq_worker.py#L134) の `fetch_and_analyze_task`

| フェーズ | 処理内容 | 実行場所 |
|---|---|---|
| Phase 1 | キーワード取得 — DB から `Keyword` を全件 or 指定 ID で SELECT | taskiq_worker.py L168-185 |
| Phase 2 | RSS フェッチ — キーワードごとに Google News RSS を取得、URL デコード、重複排除、記事 INSERT + keyword リンク作成 | news_fetcher.py `fetch_news_for_keywords()` |
| Phase 3 | 本文抽出 — `content_fetched_at == None` かつ `content_fetch_attempts < 3` の記事を対象に newspaper4k で抽出 | content_extractor.py `extract_contents()` |
| Phase 4 | AI 分析 — 未分析記事を最大 10件ずつ Gemini API で分析、翻訳・投資カテゴリ付与 | ai_analyzer.py `analyze_articles()` |
| Phase 5 | Embedding 生成 — embedding が NULL の記事にベクトルを生成 | taskiq_worker.py L263-285 |

各フェーズは独立した `try/except` で包まれ、1フェーズ失敗でも後続は継続。

### キーワードとの紐づけ方法

[news_fetcher.py:83-100](backend/app/services/news_fetcher.py#L83-L100) の `_link_keyword_to_article` 関数で `news_keywords` に INSERT。既存チェック付きの upsert 相当。

リンク INSERT は3箇所で発生:
1. Pre-decode で既存と判明した記事（L189-198）
2. Post-decode で既存と判明した記事（L206-214）
3. 新規記事の INSERT 後（L236-237）

### エラーハンドリング・リトライ

| レベル | 方式 |
|---|---|
| タスク全体 | `max_retries=3`, `retry_on_error=True`（Taskiq SimpleRetryMiddleware） |
| RSS フェッチ | リトライなし。HTTP エラー/接続失敗はスキップし次回実行を待つ |
| 本文抽出 | リトライなし。`content_fetch_attempts` をインクリメントし、3回未満なら次回再試行 |
| AI 分析 | RateLimitError で `break`（バッチ中断）、その他エラーで `continue`（記事スキップ） |

### content_fetch_attempts の扱い

- エラー時のみ `+1` インクリメント（[content_extractor.py:237](backend/app/services/content_extractor.py#L237)）
- 次回取得条件: `content_fetched_at == None` AND `content_fetch_attempts < 3`
- 成功時・空本文時は `content_fetched_at` がセットされるため再取得対象外になる
- 3回失敗した記事は永久にスキップ

---

## 2. DB スキーマ

### news_articles テーブル

| カラム名 | 型 | Nullable | デフォルト | 備考 |
|---|---|---|---|---|
| `id` | INTEGER (PK) | NO | auto | |
| `title_original` | VARCHAR(500) | NO | — | |
| `description_original` | TEXT | YES | NULL | |
| `url` | VARCHAR(2048) | NO | — | UNIQUE, INDEX |
| `source` | VARCHAR(100) | NO | — | **常に `"Google News"` 固定** (後述) |
| `published_at` | TIMESTAMPTZ | YES | NULL | INDEX |
| `fetched_at` | TIMESTAMPTZ | NO | `now(UTC)` | INDEX |
| `content` | TEXT | YES | NULL | |
| `content_fetched_at` | TIMESTAMPTZ | YES | NULL | |
| `content_fetch_attempts` | INTEGER | NO | 0 | |
| `embedding` | Vector(768) | YES | NULL | |

インデックス: `idx_news_published` (published_at), `idx_news_fetched` (fetched_at), url カラム個別

#### source カラムの実態

[news_fetcher.py:24](backend/app/services/news_fetcher.py#L24) で定数定義:
```python
DEFAULT_SOURCE = "Google News"
```

[news_fetcher.py:229](backend/app/services/news_fetcher.py#L229) の INSERT で常にこの固定値をセット:
```python
source=DEFAULT_SOURCE,  # 常に "Google News"
```

feedparser の `entry.source` は一切参照していない。つまり **全レコードが `"Google News"` という単一値**。移行時のデータクレンジングは不要だが、`source` カラムの設計意図（実際のメディア名を格納する想定）と実装が乖離している。

### news_keywords テーブル（M:N 中間テーブル）

| カラム名 | 型 | 制約 |
|---|---|---|
| `id` | INTEGER (PK) | |
| `news_article_id` | INTEGER, NOT NULL | FK → news_articles.id ON DELETE CASCADE |
| `keyword_id` | INTEGER, NOT NULL | FK → keywords.id ON DELETE CASCADE |

UNIQUE: `(news_article_id, keyword_id)`

### keywords テーブル

| カラム名 | 型 | Nullable | 備考 |
|---|---|---|---|
| `id` | INTEGER (PK) | NO | |
| `keyword` | VARCHAR(200) | NO | UNIQUE |
| `created_at` | TIMESTAMPTZ | NO | default: now(UTC) |
| `updated_at` | TIMESTAMPTZ | NO | default: now(UTC)、onupdate なし |

### keyword_categories テーブル

[keyword_category.py](backend/app/models/keyword_category.py) で定義。翻訳は `keyword_category_translations` テーブルで管理。

### keyword_category_links テーブル（M:N 中間テーブル）

| カラム名 | 型 | 制約 |
|---|---|---|
| `id` | INTEGER (PK) | |
| `keyword_id` | INTEGER, NOT NULL | FK → keywords.id ON DELETE CASCADE |
| `category_id` | INTEGER, NOT NULL | FK → keyword_categories.id ON DELETE CASCADE |

UNIQUE: `(keyword_id, category_id)`

### 外部キーと CASCADE まとめ

全ての外部キーに `ondelete="CASCADE"` が設定されている。

---

## 3. スケジューラ

### 定期実行スケジュール

- 環境変数 `fetch_interval_hours`（デフォルト: 12時間）→ [config.py:16](backend/app/config.py#L16)
- cron 式に変換: `"0 */12 * * *"`（0時・12時）→ [taskiq_worker.py:60-69](backend/app/tasks/taskiq_worker.py#L60-L69)
- 受付可能値: `{1, 2, 3, 4, 6, 8, 12, 24}`（24の約数のみ）

### タスクパラメータ

```python
async def fetch_and_analyze_task(
    keyword_ids: list[int] | None = None,  # None = 全キーワード対象
    ctx: Context = TaskiqDepends(),
)
```

### タイムアウト

- タスクタイムアウト: **30分** (`timeout=1800`)
- 結果保持: **1時間** (`result_ex_time=3600`)

---

## 4. API エンドポイント

### POST /news/fetch

[news.py:364-380](backend/app/routers/news.py#L364-L380)

**リクエスト:** `NewsFetchRequest` — ボディ全体省略可能

```json
{ "keywordIds": [1, 2, 3] }   // 省略時は全キーワード
```

**レスポンス:** `202 Accepted`

```json
{
  "message": "Fetch task submitted",
  "keywordsCount": 3,
  "jobId": "uuid-string"
}
```

**ディスパッチ:**

```python
task = await fetch_and_analyze_task.kiq(keyword_ids=keyword_ids)
```

Taskiq の `.kiq()` で Redis ブローカーにエンキューし、即座にジョブ ID を返す。

---

## 5. フロントエンド

### 記事取得時のクエリパラメータ

[types/index.ts:17-29](frontend/src/types/index.ts#L17-L29) の `NewsQuery`:

```typescript
export interface NewsQuery {
  keywordId?: number;
  kwCategoryId?: number;
  myKeywords?: boolean;
  sentiment?: Sentiment;
  minImpact?: number;
  category?: string;
  sortBy?: "publishedAt" | "impactScore";
  sortOrder?: "asc" | "desc";
  page?: number;
  perPage?: number;
  locale?: string;
}
```

[api-client.ts:91-105](frontend/src/lib/api-client.ts#L91-L105) で `URLSearchParams` に変換して `GET /news` に送信。

### フィルタリング UI の構成

| コンポーネント | フィルタ項目 | 実装方式 |
|---|---|---|
| `NewsFilters.tsx` | Sentiment, Sort by, Investment (slug), Order | セレクトボックス → URL searchParams |
| `CategorySidebar.tsx` | kwCategoryId, keywordId, myKeywords | サイドバーリンク → URL searchParams |

**`source` によるフィルタリングは未実装。** `source` は表示のみ（下記参照）。

**フィルタの二重構造に注意:** `NewsFilters` の Investment (category slug) と `CategorySidebar` の kwCategoryId は別系統のフィルタが共存している。

### source フィールドの表示

`source` は以下の3箇所で **プレーンテキストとして表示のみ**（クリックフィルタリング不可）:

| ファイル | 用途 | 表示形式 |
|---|---|---|
| `NewsCard.tsx` L48 | カード一覧のメタ情報 | `{source} · {date}` |
| `NewsDetail.tsx` L42 | 記事詳細のソース表示 | `<span>{source}</span>` |
| `watchlist/page.tsx` L72 | ウォッチリスト一覧 | メタ情報 |

### 手動フェッチ UI

`FetchButton.tsx` コンポーネントが実装済みだが、**現在どのページにも配置されていないデッドコード**。`settings/page.tsx` は `redirect("/")` のリダイレクトのみ。

---

## 6. 移行時の影響箇所まとめ

### 変更が必要なファイル

| ファイル | 変更内容 |
|---|---|
| `backend/app/services/news_fetcher.py` | URL 構築をキーワード検索 → フィード URL 直接取得に変更。Google News URL デコード不要に |
| `backend/app/models/news.py` | `feed_id` カラム追加（`rss_feeds` への FK） |
| `backend/app/tasks/taskiq_worker.py` | Phase 1 をキーワード取得 → フィード一覧取得に変更 |
| `backend/app/routers/news.py` | `POST /news/fetch` のパラメータ変更（`keyword_ids` → `feed_ids` or 全フィード） |
| `backend/app/schemas/news.py` | `NewsFetchRequest` の変更、`NewsResponse` に `feedId` 追加検討 |
| `backend/app/services/ai_analyzer.py` | AI 分析時に `keywords` テーブルの小分類タグを記事に付与するロジック追加 |
| `backend/app/config.py` | フィード購読関連の設定追加 |
| `frontend/src/types/index.ts` | `NewsQuery` に `feedId` 等の追加 |
| `frontend/src/lib/api-client.ts` | 新 API パラメータ対応 |
| `frontend/src/components/news/NewsCard.tsx` | `source` 表示をフィード名に変更 |
| `frontend/src/components/news/NewsDetail.tsx` | 同上 |
| `frontend/src/components/layout/CategorySidebar.tsx` | フィードベースのナビゲーション追加検討 |

### 新規作成が必要なファイル

| ファイル | 内容 |
|---|---|
| `backend/app/models/rss_feed.py` | `rss_feeds` テーブル定義（id, url, name, category_id, is_active, etc.） |
| `backend/app/routers/rss_feeds.py` | フィード管理用 CRUD エンドポイント |
| `backend/app/schemas/rss_feed.py` | Pydantic スキーマ |
| `backend/alembic/versions/xxx_add_rss_feeds.py` | マイグレーション |
| `frontend/src/components/feeds/` | フィード管理 UI（追加・編集・削除・有効/無効切替） |

### 既存テーブルへの影響

| テーブル | 変更 |
|---|---|
| `news_articles` | `feed_id` カラム追加（FK → rss_feeds.id）。`source` カラムは `rss_feeds.name` から自動設定に変更 |
| `keywords` | テーブル自体は残す（AI 分析時のタグ付けに使用）。検索トリガーとしての役割は廃止 |
| `news_keywords` | 役割変更 — 検索結果の紐づけから、AI が付与したタグの紐づけに変更 |

### 削除すべき既存コード

| 対象 | 理由 |
|---|---|
| `_build_rss_url()` (news_fetcher.py L40-42) | Google News RSS 検索用。不要に |
| `GOOGLE_NEWS_RSS_URL` 定数 (news_fetcher.py L21-23) | 同上 |
| `DEFAULT_SOURCE = "Google News"` 定数 (news_fetcher.py L24) | source はフィード名から取得に変更 |
| `_decode_google_urls()` (news_fetcher.py) | Google News リダイレクト URL デコード。不要に |
| **`backend/app/services/url_decoder.py` ファイル全体** | Google News URL デコード専用モジュール（79行）。`googlenewsdecoder` パッケージへの唯一の依存箇所 |
| `googlenewsdecoder` パッケージ (pip依存) | `url_decoder.py` 削除に伴い不要に |
| Pre-decode / Post-decode の2段階重複チェック | フィード URL は直接取得のため1段階で十分 |

---

## 7. 懸念事項・確認が必要な点

### 優先度順の設計判断

設計判断を依存関係に基づいて3段階に整理する。上位の決定が下位に影響するため、この順序で決定すること。

#### Tier 1: 最優先（rss_feeds テーブル設計を決定する）

**D-1. フィードとカテゴリの紐づけ（1:N vs M:N）**
- `rss_feeds` テーブルに `category_id` (FK → keyword_categories.id) を持たせるのが自然
- 1フィード = 1カテゴリ（1:N）で十分か、M:N が必要か
- → テーブル設計の根幹。最初に決定が必要

**D-2. フィードごとの取得頻度の差異**
- 全フィード一律 12時間で良いか
- フィードごとに `fetch_interval_minutes` を持たせるか
- → `rss_feeds` テーブルのカラム設計に影響

**D-3. フィードの死活監視カラム設計**
- `is_active` フラグの自動切り替え（連続 N 回失敗で自動無効化？）
- `last_fetched_at` / `last_error` / `consecutive_errors` カラムの要否
- → `rss_feeds` テーブルのカラム設計に影響

#### Tier 2: テーブル設計確定後（news_articles との関係を決定する）

**D-4. `source` カラムの扱い**（D-1 に依存）
- 現在は全レコード `"Google News"` 固定。実質無意味な値
- 選択肢: (a) `feed_id` FK を追加し `source` は `rss_feeds.name` から自動セット、(b) `source` を廃止して JOIN で取得
- 既存データは全て `"Google News"` なのでクレンジング不要。(a) が安全
- UI 3箇所（NewsCard, NewsDetail, watchlist）の表示に影響

**D-5. 既存記事の `feed_id` backfill 方針**
- `feed_id` カラム追加時、既存記事は `NULL` になる
- `nullable=True` で追加 → データ移行後 `nullable=False` に変更の2段階か
- 既存記事（全て Google News 経由）に対して feed_id を割り当てる必要があるか、NULL のまま残すか

#### Tier 3: 実装フェーズで決定可能（後回し可能）

**D-6. `keywords` テーブルの役割変更**
- 現在: 検索トリガー（キーワード → Google News 検索）
- 移行後: AI 分析時のタグ付けラベル（記事 → AI が該当キーワードを付与）
- `news_keywords` の INSERT ロジックが news_fetcher.py → ai_analyzer.py に移動

**D-7. 既存の `news_keywords` データの扱い**
- 移行前: キーワード検索でヒットした = 紐づけ
- 移行後: AI が判定して付与 = 紐づけ
- 既存リンクをそのまま残すか、AI で再タグ付けするか
- → マイグレーション時に決められる

**D-8. 重複記事の検出方法**
- 現在は `url` の UNIQUE 制約で重複排除
- 複数フィードから同一記事が配信される可能性 → URL ベースの排除で十分か
- → 実装フェーズで検証可能

**D-9. フィード取得の並行制御**
- 現在は `httpx.AsyncClient` でキーワードごとに順次フェッチ
- フィード購読型ではフィード数が固定的なので `asyncio.gather` で並列化が可能
- 同一ドメインへのレートリミット（`DomainRateLimiter`）の適用範囲を検討
- → 実装時の最適化として対応

**D-10. 移行期間の運用**
- キーワード検索型とフィード購読型を並行稼働させる期間が必要か
- `rss_feeds` テーブルの初期データ投入方法（シードマイグレーション or 管理画面）

### フロントエンド追加の影響箇所

前回のレポートで薄かったフロントエンド影響を補足する。

#### フィード管理 UI（新規）
- フィードの追加・編集・削除・有効/無効切替
- フィード一覧ページ or 設定ページ内のセクション
- 現在 `settings/page.tsx` は `redirect("/")` のリダイレクトのみなので、ここに配置するのが自然

#### フィルタリング UI の変更
- **`NewsFilters.tsx`**: `source` フィルタは現在未実装。フィード名 or フィード ID でのフィルタリング追加が検討対象
- **`CategorySidebar.tsx`**: 現在 `kwCategoryId` / `keywordId` でフィルタ。フィードとカテゴリの紐づけ方式（D-1）次第でナビゲーション構造が変わる
- **フィルタの二重構造**: `NewsFilters` の Investment (category slug) と `CategorySidebar` の kwCategoryId が別系統で共存。移行時にフィルタ体系を整理する機会

#### デッドコードの整理
- `FetchButton.tsx` — 実装済みだがどこにも配置されていない。移行後の用途を決定（フィード単位の手動フェッチに転用？ or 削除？）
- `settings/page.tsx` — リダイレクトのみ。フィード管理 UI の配置先候補
