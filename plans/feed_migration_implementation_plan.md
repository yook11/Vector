# フィード購読型移行 — 実装プラン

## 概要

Vector の記事取得方式を「キーワード検索型（Google News RSS）」から「ソース購読型（RSS + API）」に移行する。本ドキュメントは、現状調査・リサーチ結果・テーブル設計評価に基づき確定した設計判断と、3フェーズの実装計画をまとめたものである。

---

## データアーキテクチャ

ニュースソースの種類に関わらず、記事は全て `news_articles` に統一格納する。`news_sources` は「どこから・どの方法で取得するか」の設定マスタであり、記事データは入らない。

```
keyword_categories (大分類マスタ)
  │
  ├── 1:N ── news_sources (ソース設定マスタ)
  │            │
  │            └── 1:N ── news_articles (記事データ)
  │                        │
  │                        └── M:N ── keywords (小分類タグ)
  │                              via news_keywords
  │
  └── M:N ── keywords (小分類タグ)
        via keyword_category_links
```

記事の `source_id` による追跡:

| 取得方法 | `source_id` | `source`（表示用） |
|---------|------------|-------------------|
| RSS フィード経由 | ソース ID | `"TechCrunch AI"` 等（`news_sources.name` からコピー） |
| API 経由 | ソース ID | `"Hacker News"` 等（同上） |
| 移行前の既存データ | `NULL` | `"Google News"` |

---

## 確定済み設計判断（Tier 1）

| ID | 判断事項 | 結論 | 根拠 |
|----|---------|------|------|
| D-1 | フィードとカテゴリの紐づけ | **1:N**（`news_sources.category_id` FK） | カテゴリ別 RSS を購読するため 1ソース=1カテゴリで十分。M:N が必要なケースが調査で見つからなかった |
| D-2 | ソースごとの取得頻度 | `fetch_interval_minutes` + `next_fetch_at` 方式。初期は一律 720分（12時間） | `next_fetch_at` ベースにより cron 式の計算が不要。ソース追加時に即スケジュールに乗る。個別変更は管理画面/API から可能 |
| D-3 | ソースの死活監視 | `consecutive_errors` + `last_error_message`。自動無効化なし、UI 警告のみ | ソース数が少ない（10-20件）段階では手動管理で十分。一時的障害での全ソース無効化リスクを回避 |

### テーブル設計方針（評価レポートに基づき確定）

| 判断事項 | 結論 | 根拠 |
|---------|------|------|
| テーブル構造 | **Option B: 専用カラム + nullable** | Vector 既存パターン（JSONB 0件）との一貫性。本番 RSS リーダー全3製品が同方式。JSONB の更新コスト・ミューテーション検知バグ・Alembic 非対応を回避 |
| テーブル名 | `news_sources`（`rss_feeds` から変更） | RSS 以外のソース（API）も統一管理するため |
| `source_type` の管理 | VARCHAR + アプリ側 Enum | 取得方法の追加は必ずコード変更を伴う。DB テーブル化のメリットなし |
| RSS/API 固有カラム | 専用 nullable カラム | JSONB 不使用。取得方法が増えた場合は `ALTER TABLE ADD COLUMN` で対応 |

### Tier 2 判断（テーブル設計確定後）

| ID | 判断事項 | 方針 |
|----|---------|------|
| D-4 | `source` カラムの扱い | 維持し `news_sources.name` から自動セット。JOIN を避けた非正規化。既存データは全て `"Google News"` なのでクレンジング不要 |
| D-5 | 既存記事の `source_id` backfill | `NULLABLE` で追加、既存は `NULL` のまま。`ON DELETE SET NULL`。2段階マイグレーション不要 |

### Tier 3 判断（実装フェーズで決定）

| ID | 判断事項 | 方針 |
|----|---------|------|
| D-6 | `keywords` テーブルの役割変更 | 検索トリガー → AI タグ付けラベルに変更。`news_keywords` の INSERT が `news_fetcher.py` → `ai_analyzer.py` に移動。**Phase A で `news_fetcher.py` から削除し、Phase B-2 まで `news_keywords` INSERT の空白期間を許容する** |
| D-7 | 既存 `news_keywords` データ | そのまま残す（マイグレーション時に最終判断） |
| D-8 | 重複記事の検出方法 | `guid` ベース（URL fallback 付き）。**Phase A で `news_articles` に `guid` カラムを追加する** |
| D-9 | フィード取得の並行制御 | `asyncio.gather` で並列化検討。実装時の最適化として対応 |
| D-10 | 移行期間の運用 | シードマイグレーションで初期ソース投入。並行稼働期間の要否は実装時に判断 |

---

## `news_sources` テーブル設計

```
news_sources
│
│  ── 共通カラム ──
├── id                      SERIAL PK
├── name                    VARCHAR(200) NOT NULL        -- "TechCrunch AI"
├── source_type             VARCHAR(20) NOT NULL         -- "rss" / "api"
├── site_url                VARCHAR(2048) NULLABLE       -- 媒体ホームページ
├── category_id             INTEGER NOT NULL FK → keyword_categories.id
├── is_active               BOOLEAN NOT NULL DEFAULT TRUE
├── fetch_interval_minutes  INTEGER NOT NULL DEFAULT 720 -- D-2 確定: 初期一律12時間
├── next_fetch_at           TIMESTAMPTZ NULLABLE
├── last_fetched_at         TIMESTAMPTZ NULLABLE
├── consecutive_errors      INTEGER NOT NULL DEFAULT 0
├── last_error_message      TEXT NULLABLE
│
│  ── RSS 固有（API レコードでは NULL）──
├── feed_url                VARCHAR(2048) NULLABLE UNIQUE -- RSS エンドポイント
├── etag                    VARCHAR(256) NULLABLE         -- HTTP ETag
├── last_modified_header    VARCHAR(256) NULLABLE         -- HTTP Last-Modified
│
│  ── API 固有（RSS レコードでは NULL）──
├── api_endpoint            VARCHAR(200) NULLABLE         -- "hacker-news" / "alpha-vantage"
│
├── created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
├── updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
```

### CHECK 制約

```sql
-- source_type の値を制限
CHECK (source_type IN ('rss', 'api'))

-- RSS なら feed_url 必須、API なら api_endpoint 必須
CHECK (
  (source_type = 'rss' AND feed_url IS NOT NULL)
  OR
  (source_type = 'api' AND api_endpoint IS NOT NULL)
)

-- fetch_interval_minutes の下限・上限
CHECK (fetch_interval_minutes BETWEEN 15 AND 1440)
```

### source_type のアプリ側管理

```python
class SourceType(str, Enum):
    RSS = "rss"
    API = "api"
    # 将来: SCRAPER = "scraper"
```

### インデックス

```sql
-- スケジューラ用: 取得対象ソースの検索（部分インデックス）
CREATE INDEX idx_sources_active_next_fetch
    ON news_sources (next_fetch_at) WHERE is_active = TRUE;

-- ソース別記事検索
CREATE INDEX idx_articles_source_published
    ON news_articles (source_id, published_at DESC) WHERE source_id IS NOT NULL;
```

### `news_articles` テーブルへの変更

```
+ source_id    INTEGER NULLABLE FK → news_sources.id ON DELETE SET NULL
+ guid         VARCHAR(2048) NULLABLE UNIQUE  -- RSS entry.id / entry.guid
```

### `next_fetch_at` のライフサイクル

| 状態 | 動作 |
|------|------|
| ソース新規登録 | `next_fetch_at = NULL` → 即取得対象 |
| 取得成功 | `next_fetch_at = now + interval`, `consecutive_errors = 0`, etag/last_modified 更新 |
| 取得失敗 | `next_fetch_at = now + interval`, `consecutive_errors += 1`, `last_error_message` 更新 |
| 手動無効化 | `is_active = false` → スケジューラ対象外 |
| 手動再有効化 | `is_active = true`, `next_fetch_at = NULL` → 即取得 |

スケジューラのクエリ:

```sql
SELECT * FROM news_sources
WHERE is_active = TRUE
  AND (next_fetch_at IS NULL OR next_fetch_at <= now())
ORDER BY next_fetch_at ASC NULLS FIRST;
```

---

## タグ付け戦略: ハイブリッド方式

| レイヤー | 何を決定するか | コスト | タイミング |
|---------|--------------|--------|----------|
| ソースレベル | 大分類（Technology, Biotech, Crypto...） | 無料 | 取得即時 |
| AI レベル | 小分類タグ（specific keywords） | API コスト | Phase 4 分析時 |

- 記事取得時: ソースの `category_id` を記事に自動継承
- AI 分析時: AI が記事内容を分析 → `keywords` テーブルからタグ付与
- `news_keywords` の INSERT ロジックが `news_fetcher.py` → `ai_analyzer.py` に移動
- **Phase A 完了〜Phase B-2 完了の間は `news_keywords` INSERT が停止する（空白期間）。** カテゴリフィルタは `news_sources.category_id` 経由で代替可能

---

## RSS フィード調査結果サマリー

### 即導入可能（ペイウォールなし・RSS 充実）— Tier 1

| ソース | フィード URL | カテゴリ | 備考 |
|--------|------------|---------|------|
| TechCrunch | `techcrunch.com/feed/` | Technology | カテゴリ別 RSS 30+ あり |
| FierceBiotech | `fiercebiotech.com/rss/xml` | Biotech | カテゴリ別あり |
| BioPharma Dive | `biopharmadive.com/feeds/news/` | Biotech | トピック別あり |
| The Quantum Insider | `thequantuminsider.com/feed/` | Quantum | 全文配信 |
| Cointelegraph | `cointelegraph.com/rss` | Crypto | カテゴリ別充実 |
| Yahoo Finance | `finance.yahoo.com/news/rssindex` | Finance | ティッカー別あり |
| ITmedia | `rss.itmedia.co.jp/rss/2.0/itmedia_all.xml` | Technology/JP | カテゴリ別充実 |

### ヘッドライン監視向き（ペイウォールあり）

| ソース | フィード URL | カテゴリ | 備考 |
|--------|------------|---------|------|
| Ars Technica | `feeds.arstechnica.com/arstechnica/index` | Technology | 抜粋のみ |
| The Verge | `theverge.com/rss/index.xml` | Technology | プレビューのみ |
| STAT News | `statnews.com/feed/` | Biotech | 40-50% が有料 |
| Endpoints News | `endpoints.news/feed/` | Biotech | 従量制ペイウォール |
| CoinDesk | `coindesk.com/arc/outboundfeeds/rss/` | Crypto | カテゴリ別は 403 |
| The Block | `theblock.co/rss.xml` | Crypto | 抜粋のみ |
| Seeking Alpha | `seekingalpha.com/feed.xml` | Finance | タイトル+リンクのみ |
| 日経クロステック | `xtech.nikkei.com/rss/index.rdf` | Technology/JP | 記事は有料 |
| EE Times Japan | `rss.itmedia.co.jp/rss/2.0/eetimes.xml` | Semiconductor/JP | ITmedia 傘下 |

### 導入困難・非推奨

| ソース | 理由 |
|--------|------|
| Sifted | 403 エラー（FT 系ペイウォール） |
| Reuters | RSS 完全廃止（2020年6月） |

### RSS 以外の補完ソース

| ソース | コスト | 用途 |
|--------|-------|------|
| Hacker News API | 無料・制限なし | Tech/Startup 補完 |
| Alpha Vantage News API | 無料（25 req/日） | 金融ニュース補完 |
| CoinDesk Data API | 無料（250K 生涯） | Crypto 価格データ連携 |

---

## 実装フェーズ

### 前提条件（Phase A 着手前に完了すべき事項）

| # | 事項 | 理由 |
|---|------|------|
| P-1 | 未コミット変更の整理・コミット | 特にシードマイグレーション `f52d4ecebe6b`（72キーワード）が未コミット。この状態で `news_sources` マイグレーションを追加すると Alembic リビジョンチェーンが複雑化する |
| P-2 | `CategorySidebar.tsx`（新規）のコミット | Phase C-3 のフィルタ整理対象。安定化しておく |
| P-3 | `keyword_categories` ルーター/スキーマ変更のコミット | カテゴリ別記事数取得の追加。`news_sources.category_id` FK の受け皿として機能予定 |

### Phase A: DB 基盤 + ソース取得の置き換え（コア）

**目標:** キーワード検索型 → ソース購読型への切り替え完了

#### A-1: `news_sources` テーブル作成 ✅ 完了

**対象ファイル:**
- 新規: `backend/app/models/news_source.py` — SQLModel モデル + `SourceType` Enum
- 新規: `backend/alembic/versions/a1_add_news_sources.py`
- 変更: `backend/app/models/keyword_category.py` — `sources: list["NewsSource"]` 逆方向 Relationship 追加
- 変更: `backend/app/models/__init__.py` — `NewsSource`, `SourceType` を re-export

**注意事項:**
- ⚠️ CHECK 制約は Alembic `autogenerate` で自動検出されない。マイグレーションファイルに手動記述が必要
- 部分インデックス `idx_sources_active_next_fetch` もマイグレーションに手動記述

#### A-2: `news_articles` テーブル変更 ✅ 完了

**対象ファイル:**
- 変更: `backend/app/models/news.py`
- 新規: `backend/alembic/versions/a2_add_source_id_and_guid_to_news_articles.py`

**追加カラム:**
- `source_id: int | None` — NULLABLE FK → `news_sources.id`, ON DELETE SET NULL
- `guid: str | None` — VARCHAR(2048), NULLABLE, UNIQUE（RSS の `entry.id` / `entry.guid` を格納）
- `source_ref: "NewsSource"` — Relationship 追加（`sa_relationship_kwargs={"uselist": False}`）

**実装時の差分:**
- Relationship の型アノテーションは `"NewsSource | None"` ではなく `"NewsSource"` を使用。SQLAlchemy の forward reference 解決が `| None` 構文を解釈できないため。nullable は FK 側の `ON DELETE SET NULL` で制御。

**既存データへの影響:** `source_id` = NULL, `guid` = NULL のまま。クレンジング不要。

**インデックス:** `idx_articles_source_published` (source_id, published_at DESC) WHERE source_id IS NOT NULL

#### A-3: `news_fetcher.py` 書き換え ✅ 完了

**対象ファイル:** `backend/app/services/news_fetcher.py` (旧251行 → 296行に書き換え)

**入出力の設計:**
- **入力:** `list[NewsSource]`（A-4 の Phase 1 から受け取る）
- **出力:** `FetchResult`（`source_results: list[SourceFetchResult]` を含む）— ソースごとの成功/失敗/新規件数/スキップ件数/etag/last_modified（A-5 のステータス更新に使用）

**実装済み内容:**
1. `fetch_news_for_sources(session, sources)` — 新メイン関数
2. `_fetch_rss_source(client, session, source)` — 個別ソースの取得・パース・記事作成
3. `_extract_guid(entry)` — `entry.id` → `entry.link` のフォールバックで GUID 抽出
4. `_extract_full_content(entry)` — RSS 全文配信（feedparser が `content:encoded` を `entry.content` に正規化）を検出し即格納（500文字超のヒューリスティック）
5. `SourceFetchResult` dataclass — `source_id`, `success`, `new_count`, `skipped_count`, `error_message`, `etag`, `last_modified`, `not_modified`
6. Conditional GET: `httpx` で `If-None-Match` / `If-Modified-Since` ヘッダーを付与。304 時は即 return
7. 重複排除: `guid` ベース + URL フォールバックの1段階バッチチェック（チャンクサイズ 500）
8. `source` カラム: `news_sources.name` を自動セット（D-4 確定済み）
9. `source_id`, `guid` カラム: 記事作成時に自動セット
10. API ソース（`source_type != "rss"`）は Phase C まで未対応。ログ警告のみ

**後方互換シム:**
- `fetch_news_for_keywords(session, keywords)` — `taskiq_worker.py` が依存しているため残置。keywords 引数は無視し、全アクティブソースを `next_fetch_at` ベースでクエリして `fetch_news_for_sources()` に委譲。**A-4 実装時に削除する。**

**feedparser の継続利用:**
- 現行パターン維持: `httpx.get()` → `feedparser.parse(response.text)`
- Conditional GET は `httpx` 側で処理（feedparser の etag/modified パラメータは使わない）

#### A-4: `taskiq_worker.py` Phase 1-2 書き換え ✅ 完了

**対象ファイル:** `backend/app/tasks/taskiq_worker.py` (旧289行 → 332行に書き換え)

**実装済み内容:**

1. **cron 間隔の短縮** — `config.py`: `fetch_interval_hours: int = 12` → `check_interval_minutes: int = 30`。`_FETCH_CRON = "*/30 * * * *"`。バリデーション: `_VALID_INTERVAL_MINUTES = {5, 10, 15, 20, 30, 60}`（60の約数）
2. **Phase 1 書き換え** — `select(Keyword)` → `select(NewsSource).where(is_active, or_(next_fetch_at IS NULL, next_fetch_at <= now())).order_by(next_fetch_at ASC NULLS FIRST)`。`source_ids` 指定時は `NewsSource.id.in_(source_ids)` でフィルタ
3. **Phase 2 書き換え** — `fetch_news_for_keywords()` → `fetch_news_for_sources()`
4. **関数シグネチャ** — `keyword_ids: list[int] | None` → `source_ids: list[int] | None`
5. **result dict** — `keywords_count` → `sources_count`
6. **import** — `Keyword` → `NewsSource`、`fetch_news_for_keywords` → `fetch_news_for_sources`、`func`・`or_` 追加
7. **後方互換シムの削除** — `news_fetcher.py` から `fetch_news_for_keywords()` を削除
8. **Phase 3-5** — 変更なし（独立セッションで動作するため影響なし）

**テスト変更:**
- `tests/test_taskiq_worker.py` — 全6テストを `NewsSource` モック・`fetch_news_for_sources`・`SourceFetchResult`・`sources_count` に書き換え

#### A-5: スケジューラ更新 — `next_fetch_at` 更新ロジック ✅ 完了

**対象ファイル:** `backend/app/tasks/taskiq_worker.py`（A-4 と同一ファイル内、Phase 2 の try ブロック内に実装）

**実装済み内容:**
- Phase 2 の `fetch_news_for_sources()` 完了後、`FetchResult.source_results` をイテレーションし各ソースを更新
- `source` オブジェクトを直接変更 → `session.add(source)` → `session.commit()`

| 状態 | 更新内容 |
|------|---------|
| 成功 | `next_fetch_at = now + fetch_interval_minutes`, `last_fetched_at = now`, `consecutive_errors = 0`, `etag`/`last_modified_header` 更新（非 None の場合のみ） |
| 失敗 | `next_fetch_at = now + fetch_interval_minutes`, `last_fetched_at = now`, `consecutive_errors += 1`, `last_error_message` 更新 |
| 304 Not Modified | `next_fetch_at = now + fetch_interval_minutes`, `last_fetched_at = now`, `consecutive_errors = 0`（etag/last_modified は変更なし） |

#### A-6: `POST /news/fetch` 更新 ✅ 完了

**対象ファイル:**
- 変更: `backend/app/schemas/news.py` (L46-67) — スキーマ更新
- 変更: `backend/app/routers/news.py` (L364-380) — エンドポイント更新
- 変更: `frontend/src/types/generated.ts` — 型定義更新
- 変更: `backend/tests/test_routers/test_news.py` — テスト更新

**実装済み内容:**
- `NewsFetchRequest.keyword_ids` → `source_ids: list[int] | None`
- `NewsFetchResponse.keywords_count` → `sources_count: int | None`
- `fetch_and_analyze_task.kiq(keyword_ids=...)` → `fetch_and_analyze_task.kiq(source_ids=...)`
- `generated.ts`: `keywordIds` → `sourceIds`、`keywordsCount` → `sourcesCount`
- テスト: `test_fetch_with_keyword_ids` → `test_fetch_with_source_ids`、アサーションを `sourcesCount` / `source_ids=` に更新

**フロントエンドへの影響:**
- `triggerFetch()`（`api-client.ts` / `client-api.ts`）は型付きの `NewsFetchRequest` body をそのまま透過するため、コード変更不要。`generated.ts` の型定義更新のみで追従完了
- `FetchButton.tsx` は引数なしで `triggerFetch()` を呼ぶためフィールド名変更の影響なし

#### A-7: シードデータ ✅ 完了

**対象ファイル:** 新規 `backend/alembic/versions/a3_seed_news_sources.py`

**実装済み内容:**
- 即導入可能 7 ソースの INSERT マイグレーション（全て `source_type = "rss"`、`fetch_interval_minutes = 720`）
- `category_id` は `keyword_categories.slug` から `SELECT` で動的解決（ID ハードコードなし）
- downgrade: `feed_url` で DELETE

| ソース | feed_url | カテゴリ slug |
|--------|----------|-------------|
| TechCrunch | `techcrunch.com/feed/` | `ai_ml` |
| FierceBiotech | `fiercebiotech.com/rss/xml` | `biotech` |
| BioPharma Dive | `biopharmadive.com/feeds/news/` | `biotech` |
| The Quantum Insider | `thequantuminsider.com/feed/` | `quantum` |
| Cointelegraph | `cointelegraph.com/rss` | `fintech` |
| Yahoo Finance | `finance.yahoo.com/news/rssindex` | `fintech` |
| ITmedia | `rss.itmedia.co.jp/rss/2.0/itmedia_all.xml` | `ai_ml` |

#### A-8: 不要コード削除 ✅ 完了

| 対象 | ファイル / 場所 | 状態 |
|------|---------------|------|
| `GOOGLE_NEWS_RSS_URL` 定数 | `news_fetcher.py` (旧 L21-23) | ✅ A-3 書き換えで削除 |
| `DEFAULT_SOURCE` 定数 | `news_fetcher.py` (旧 L24) | ✅ A-3 書き換えで削除 |
| `_build_rss_url()` | `news_fetcher.py` (旧 L40-42) | ✅ A-3 書き換えで削除 |
| `from app.services.url_decoder import decode_urls` | `news_fetcher.py` (旧 L19) | ✅ A-3 書き換えで削除 |
| `decode_urls()` 呼び出し | `news_fetcher.py` (旧 L172) | ✅ A-3 書き換えで削除 |
| Pre-decode / Post-decode の 2段階重複チェック | `news_fetcher.py` (旧 L160-186) | ✅ A-3 で guid ベース1段階に変更 |
| `_link_keyword_to_article()` | `news_fetcher.py` (旧 L83-100) | ✅ A-3 書き換えで削除 |
| `_get_existing_urls()` | `news_fetcher.py` (旧 L74-80) | ✅ A-3 で guid ベースに変更 |
| `url_decoder.py` **ファイル全体** | `services/url_decoder.py` (79行) | ✅ ファイル削除 |
| `googlenewsdecoder` パッケージ | `requirements.txt` | ✅ 依存削除 |
| `backfill_decoded_urls.py` | `scripts/backfill_decoded_urls.py` (139行) | ✅ ファイル削除（`url_decoder` 依存の一回限りスクリプト） |
| `tests/test_url_decoder.py` | `tests/test_url_decoder.py` (93行) | ✅ ファイル削除 |
| `tests/test_news_fetcher.py` | `tests/test_news_fetcher.py` (382行) | ✅ 新 API 向けに全面書き直し（9テスト） |

**テスト変更の詳細:**
- `conftest.py` に `NewsSource` インポートと `sample_source` fixture 追加
- 旧テスト: `fetch_news_for_keywords` + `_build_rss_url` + URL decode 系 → 全削除
- 新テスト: `fetch_news_for_sources` + `_extract_guid` + Conditional GET + 304 + full content 検出

### Phase B: 取得品質の改善

**目標:** 本文抽出の品質・効率を向上

| # | タスク | 対象ファイル |
|---|--------|------------|
| B-1 | trafilatura 導入 — `newspaper4k` からの移行 | `services/content_extractor.py` |
|     | フォールバックチェーン: RSS 全文 → trafilatura → newspaper4k | |
|     | ⚠️ 新パッケージ追加は CLAUDE.md の "Ask first" ルールに該当。`content_extractor.py` の大幅改修を伴う | |
| B-2 | `news_keywords` の INSERT ロジック移動（ハイブリッドタグ付け） | `services/ai_analyzer.py` |
|     | Phase A で `news_fetcher.py` から削除された `_link_keyword_to_article()` のロジックを移植 | |
| B-3 | ソース管理 API — `news_sources` CRUD エンドポイント | `routers/news_sources.py`, `schemas/news_source.py` |
| B-4 | ソース管理 UI — `settings/page.tsx` にソース管理セクション配置 | `frontend/src/app/(protected)/settings/page.tsx` |
|     | - ソース一覧表示（名前、URL/エンドポイント、カテゴリ、ステータス） | |
|     | - 追加・編集・削除 | |
|     | - 有効/無効切替 | |
|     | - `consecutive_errors >= 5` の警告表示 | |

### Phase C: ソース拡張 + 補完

**目標:** RSS 以外の無料ソースを追加、フロントエンド整理

| # | タスク | 対象ファイル |
|---|--------|------------|
| C-1 | Hacker News API 統合 — `source_type = "api"`, `api_endpoint = "hacker-news"` | 新規サービス |
| C-2 | Alpha Vantage News API 統合 — `source_type = "api"`, `api_endpoint = "alpha-vantage"` | 新規サービス |
| C-3 | フロントエンドフィルタ整理 | `NewsFilters.tsx`, `CategorySidebar.tsx` |
|     | - ソース/カテゴリベースのフィルタリング追加 | |
|     | - Investment (category slug) と kwCategoryId の二重構造解消 | |
| C-4 | `FetchButton.tsx` 活用判断 — ソース単位の手動フェッチに転用 or 削除 | `FetchButton.tsx` |
| C-5 | `source` 表示のソース名対応 | `NewsCard.tsx`, `NewsDetail.tsx`, `watchlist/page.tsx` |

---

## スコープ外（将来検討）

| 項目 | 着手条件 |
|------|---------|
| 商用 API（GNews、NewsData.io） | RSS の不足が判明してから |
| ペイウォール突破（Firecrawl 等） | ペイウォール記事の取得が必要になってから |
| ソース別取得頻度の最適化 | 運用データが溜まってから |
| `consecutive_errors` による自動無効化 | ソース数が増えてから（50+ 件目安） |
| WebSub (Push) 対応 | 対応サイトが増えてから |

---

## 技術的改善事項（リサーチからの知見）

### コンテンツ取得最適化

| 手法 | 効果 | 導入フェーズ |
|------|------|------------|
| RSS 全文検出（`content:encoded` チェック） | 全文配信ソースで HTTP リクエスト削減 | Phase A |
| Conditional GET（ETag / Last-Modified） | 未変更ソースのスキップ | Phase A |
| `guid` ベース重複排除 | URL 正規化の複雑さを回避 | Phase A |
| trafilatura 導入（F1: 0.909 vs newspaper4k: 0.713） | 本文抽出精度向上 | Phase B |

### 推奨ハイブリッドソース構成

```
┌─────────────────────────────────────────────────┐
│  Tier 1: 無料（即座に導入）                       │
│  ├── 直接 RSS 購読（7+ ソース）       ← 主力     │
│  ├── Hacker News API                 ← Tech 補完 │
│  └── Alpha Vantage News API          ← Finance   │
│                                        月額: $0   │
├─────────────────────────────────────────────────┤
│  Tier 2: 低コスト（RSS 不足時に追加）             │
│  ├── GNews.io Essential              ← 全文取得   │
│  └── CoinDesk Data API (free)        ← Crypto    │
│                                    月額: ~$55      │
├─────────────────────────────────────────────────┤
│  Tier 3: スケール時（将来検討）                    │
│  ├── NewsData.io                     ← 日本語拡張 │
│  └── NewsAPI.ai                      ← NLP 強化   │
│                                   月額: $90-300    │
└─────────────────────────────────────────────────┘
```

---

## 検証プロトコル（Phase A 完了後）

```bash
# Backend: lint + format + test
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Frontend: lint + type check
cd frontend && npx eslint src/ && npx tsc --noEmit

# Alembic: マイグレーション適用確認
cd backend && alembic upgrade head

# Docker: 全サービス起動確認
docker compose up -d && docker compose ps
```

### 手動確認項目

1. `POST /api/v1/news/fetch` でタスクがエンキューされること
2. worker ログでソース取得が実行されること（`next_fetch_at` チェック → 対象ソース取得）
3. 記事が `news_articles` に `source_id` + `guid` 付きで保存されること
4. `source` カラムに `news_sources.name` の値が入っていること（`"Google News"` ではない）
5. 取得後 `next_fetch_at` が `now + fetch_interval_minutes` に更新されること
6. 304 Not Modified 時に `consecutive_errors` が増加しないこと
7. `GET /api/v1/news` で記事が返ること
8. 既存記事（`source_id = NULL`）が引き続き正常に表示されること
