# C-1: Hacker News API 統合 — 実装仕様書

## Context

Vector のニュースソースは現在すべて RSS フィードで構成されている。Phase A で `news_sources` テーブルに `source_type = "api"` と `api_endpoint` フィールドを追加済みだが、API ソースの取得ロジックは未実装（`news_fetcher.py` でログ警告のみ）。C-1 では Hacker News を最初の API ソースとして統合し、Tech/Startup 分野のニュースを補完する。

---

## 確定済み設計判断

| 項目 | 判断 |
|------|------|
| API | Algolia HN Search API（Firebase 公式 API ではない） |
| エンドポイント | `GET https://hn.algolia.com/api/v1/search_by_date` |
| フィルタ | `tags=story`, `numericFilters=points>20` |
| 外部URLなし記事 | スキップ（Ask HN 等のテキスト投稿を除外） |
| GUID 形式 | `hn:{objectID}`（例: `hn:47139675`） |
| 重複排除 | `news_articles.url` の UNIQUE 制約で自動スキップ |
| ポイント閾値 | `config.py` で環境変数として管理（変更容易） |
| DB マイグレーション | 不要（既存スキーマで対応可能） |
| フロントエンド変更 | 不要 |

### Algolia HN Search API 選択理由

- 1リクエストで最大1,000件取得可能（Firebase は1件ずつ個別取得が必要）
- 日付・ポイントのフィルタがAPI側で可能
- 認証不要、完全無料、10,000リクエスト/時/IP
- リポジトリは2026年2月にアーカイブされたが、新コードベースで運用継続中
- Vector の定期バッチ取得パターンに最適

---

## API 仕様

### リクエスト

```
GET https://hn.algolia.com/api/v1/search_by_date
  ?tags=story
  &numericFilters=points>20,created_at_i>{last_fetched_timestamp}
  &hitsPerPage=50
```

### レスポンス構造（使用フィールドのみ）

```json
{
  "hits": [
    {
      "objectID": "47139675",
      "title": "I'm helping my dog vibe code games",
      "url": "https://www.calebleak.com/posts/dog-game/",
      "author": "cleak",
      "points": 1082,
      "num_comments": 365,
      "story_text": null,
      "created_at": "2026-02-24T17:15:17Z",
      "created_at_i": 1771953317,
      "_tags": ["story", "author_cleak", "story_47139675"]
    }
  ],
  "nbHits": 2481,
  "page": 0,
  "nbPages": 50,
  "hitsPerPage": 50
}
```

### フィールドマッピング

| Algolia レスポンス | news_articles カラム | 備考 |
|---|---|---|
| `title` | `title_original` | |
| `url` | `url` | `null` の場合はスキップ |
| `objectID` | `guid` | `hn:{objectID}` 形式で保存 |
| `created_at` | `published_at` | ISO 8601 → datetime 変換 |
| — | `source` | `news_sources.name` から自動セット（既存RSSと同じ） |
| — | `source_id` | HN 用 `news_sources` レコードの `id` |
| `created_at_i` | — | 次回取得時の `numericFilters` に使用 |
| `url` | `description_original` | null 固定（HN は description を持たない） |

### API 制限

| 項目 | 値 |
|---|---|
| レート制限 | 10,000 リクエスト/時/IP |
| 最大 hitsPerPage | 1,000 |
| 最大取得件数 | 1,000件（ページネーション含む） |
| 認証 | 不要 |
| 料金 | 無料 |

---

## 変更ファイル一覧

### 新規

| ファイル | 内容 |
|---------|------|
| `backend/app/services/hacker_news.py` | HN API クライアント |
| `backend/tests/test_hacker_news.py` | HN fetcher テスト |

### 変更

| ファイル | 変更内容 |
|---------|---------|
| `backend/app/services/news_fetcher.py` | `else` ブランチで `api_endpoint == "hacker-news"` → HN fetcher 呼出し |
| `backend/app/config.py` | HN 関連設定値追加 |
| `backend/tests/conftest.py` | API タイプの `sample_source` fixture 追加 |

### 変更不要

| ファイル | 理由 |
|---------|------|
| `NewsSource` モデル | `source_type`, `api_endpoint` フィールド既存 |
| `SourceType` enum | `API = "api"` 既存 |
| `taskiq_worker.py` | `source_type` に関係なく全ソースを `fetch_news_for_sources()` に渡すだけ |
| CRUD API / スキーマ | `source_type = "api"` + `api_endpoint` 既にサポート済み |
| フロントエンド | バックエンドのサービス追加のみで UI 変更なし |
| Alembic マイグレーション | DB 変更なし |

---

## 実装ステップ

### Step 1: config.py に設定追加

**File:** `backend/app/config.py`

```python
# Hacker News API
hn_api_base_url: str = "https://hn.algolia.com/api/v1"
hn_min_points: int = 20          # numericFilters=points>{hn_min_points}
hn_hits_per_page: int = 50       # 1リクエストあたりの取得件数
```

環境変数: `HN_MIN_POINTS`, `HN_HITS_PER_PAGE` で上書き可能。

---

### Step 2: hacker_news.py 新規作成

**File:** `backend/app/services/hacker_news.py`

#### 2a. `HackerNewsClient` クラス

```python
class HackerNewsClient:
    """Algolia HN Search API クライアント"""

    def __init__(self, http_client: httpx.AsyncClient):
        self.http_client = http_client
        self.base_url = settings.hn_api_base_url

    async def fetch_recent_stories(
        self,
        since_timestamp: int | None = None,
    ) -> list[HNStory]:
        """
        最新ストーリーを取得。

        Args:
            since_timestamp: この Unix timestamp 以降の記事のみ取得。
                             None の場合はフィルタなし（初回取得用）。

        Returns:
            HNStory のリスト（url が None のものは除外済み）
        """
```

#### 2b. `HNStory` dataclass

```python
@dataclass
class HNStory:
    object_id: str          # Algolia objectID
    title: str
    url: str                # 外部記事URL（None はフィルタ済み）
    points: int
    created_at: datetime
    created_at_i: int       # Unix timestamp（次回取得用）
    author: str
    num_comments: int
```

#### 2c. API 呼び出しロジック

```python
async def fetch_recent_stories(self, since_timestamp: int | None = None) -> list[HNStory]:
    params = {
        "tags": "story",
        "hitsPerPage": settings.hn_hits_per_page,
    }

    # numericFilters 構築
    numeric_filters = [f"points>{settings.hn_min_points}"]
    if since_timestamp:
        numeric_filters.append(f"created_at_i>{since_timestamp}")
    params["numericFilters"] = ",".join(numeric_filters)

    response = await self.http_client.get(
        f"{self.base_url}/search_by_date",
        params=params,
    )
    response.raise_for_status()
    data = response.json()

    stories = []
    for hit in data.get("hits", []):
        # 外部URLなしはスキップ（Ask HN 等）
        if not hit.get("url"):
            continue
        stories.append(HNStory(
            object_id=hit["objectID"],
            title=hit["title"],
            url=hit["url"],
            points=hit.get("points", 0),
            created_at=datetime.fromisoformat(hit["created_at"].replace("Z", "+00:00")),
            created_at_i=hit["created_at_i"],
            author=hit.get("author", ""),
            num_comments=hit.get("num_comments", 0),
        ))

    return stories
```

#### 2d. 記事変換ヘルパー

```python
async def fetch_and_save_stories(
    self,
    source: NewsSource,
    session: AsyncSession,
) -> SourceFetchResult:
    """
    HN ストーリーを取得し、news_articles に保存する。

    - guid: "hn:{objectID}" 形式
    - url の UNIQUE 制約で既存記事と自動重複排除
    - since_timestamp: source.last_fetched_at から算出
    """
```

**処理フロー:**

1. `source.last_fetched_at` を Unix timestamp に変換（初回は `None`）
2. `fetch_recent_stories(since_timestamp)` で API 呼び出し
3. 各 `HNStory` を `NewsArticle` に変換:
   - `title_original` = `story.title`
   - `url` = `story.url`
   - `guid` = `f"hn:{story.object_id}"`
   - `published_at` = `story.created_at`
   - `source` = `source.name`
   - `source_id` = `source.id`
   - `description_original` = `None`
4. `session.merge()` or INSERT with `ON CONFLICT (url) DO NOTHING`
5. `SourceFetchResult` を返す（新規保存件数、スキップ件数等）

**重複排除の仕組み:**
- `guid` の UNIQUE 制約: 同じ HN 記事の重複取得を防止
- `url` の UNIQUE 制約: RSS ソースと HN で同じ外部記事を取得した場合に重複防止

---

### Step 3: news_fetcher.py のディスパッチ追加

**File:** `backend/app/services/news_fetcher.py`（L265-278 付近）

**Before:**
```python
else:
    # API sources (Hacker News, Alpha Vantage) — Phase C
    logger.warning("unsupported_source_type", ...)
    source_result = SourceFetchResult(source_id=source.id, success=False, ...)
```

**After:**
```python
elif source.source_type == SourceType.API:
    if source.api_endpoint == "hacker-news":
        hn_client = HackerNewsClient(http_client)
        source_result = await hn_client.fetch_and_save_stories(
            source=source,
            session=session,
        )
    else:
        logger.warning(
            "unsupported_api_endpoint",
            source_id=source.id,
            api_endpoint=source.api_endpoint,
        )
        source_result = SourceFetchResult(
            source_id=source.id, success=False, ...
        )
```

**ポイント:**
- `api_endpoint` の値でディスパッチ（C-2 Alpha Vantage 追加時も同じ構造で拡張可能）
- 未知の `api_endpoint` はログ警告 + 失敗として処理
- `SourceFetchResult` は既存 dataclass をそのまま使用（`etag`, `last_modified` は `None`）

---

### Step 4: テスト

**File:** `backend/tests/test_hacker_news.py`（NEW）

#### 4a. HackerNewsClient テスト

```python
# test_fetch_recent_stories_success
# - httpx mock でレスポンスを返す
# - url=None の hit がフィルタされることを確認
# - HNStory の各フィールドが正しくマッピングされることを確認

# test_fetch_recent_stories_with_since_timestamp
# - since_timestamp が numericFilters に含まれることを確認

# test_fetch_recent_stories_api_error
# - 429 / 500 等のエラーハンドリングを確認
```

#### 4b. fetch_and_save_stories 統合テスト

```python
# test_save_new_stories
# - 新規記事が news_articles に保存されることを確認
# - guid が "hn:{objectID}" 形式であることを確認

# test_skip_duplicate_url
# - 同じ url の既存記事がある場合にスキップされることを確認

# test_skip_duplicate_guid
# - 同じ guid の記事が重複保存されないことを確認
```

**File:** `backend/tests/conftest.py`（変更）

```python
@pytest.fixture
def sample_hn_source(session: AsyncSession) -> NewsSource:
    """Hacker News API ソースの fixture"""
    source = NewsSource(
        name="Hacker News",
        source_type=SourceType.API,
        api_endpoint="hacker-news",
        is_active=True,
        fetch_interval_minutes=360,  # 6時間おき
    )
    session.add(source)
    session.commit()
    return source
```

---

## シードデータ

Settings ページから手動追加、またはマイグレーションでシード:

```python
NewsSource(
    name="Hacker News",
    source_type="api",
    api_endpoint="hacker-news",
    site_url="https://news.ycombinator.com",
    is_active=True,
    fetch_interval_minutes=360,   # 6時間おき
)
```

`fetch_interval_minutes=360` の理由: HN のフロントページは数時間で入れ替わるため、6時間おきで十分。頻度を上げても同じ記事を再取得するだけ（重複排除で自動スキップ）。

---

## エラーハンドリング

| エラー | 対応 |
|--------|------|
| HTTP 429 (Rate Limited) | ログ警告 + `SourceFetchResult(success=False)` → `consecutive_errors` 加算 → 次回リトライ |
| HTTP 5xx | 同上 |
| ネットワークエラー | 同上 |
| レスポンス JSON パースエラー | ログエラー + 失敗扱い |
| `url` が None の記事 | スキップ（正常動作、エラーではない） |
| `url` UNIQUE 違反 | `ON CONFLICT DO NOTHING`（正常動作、重複排除） |

`consecutive_errors` による自動無効化は Phase C スコープ外（ソース数 50+ になってから検討）。

---

## 後続パイプラインとの関係

HN から取得された記事は、既存の RSS 記事と完全に同じパイプラインで処理される:

1. **Phase 2 (content_extractor):** `url` から trafilatura で本文取得
2. **Phase 3 (embedding):** ベクトル生成
3. **Phase 4 (ai_analyzer):** Gemini で翻訳・要約・センチメント分析・投資カテゴリ付与
4. **Phase 5 (keyword_tagger):** キーワード自動タグ付け

追加実装は不要。`source_type` によるパイプラインの分岐はない。

---

## 検証プロトコル

```bash
# Backend: lint + format + test
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Docker 環境での手動確認
docker compose up -d

# 1. Settings ページで HN ソースを追加（または seed データ確認）
# 2. Fetch News を実行
# 3. Worker ログで HN 取得を確認
docker compose logs worker --tail=100 | grep -i "hacker"

# 4. DB で HN 記事が保存されたか確認
docker compose exec db psql -U vector -d vector -c \
  "SELECT COUNT(*), source FROM news_articles WHERE guid LIKE 'hn:%' GROUP BY source;"

# 5. RSS 記事と URL が被った場合にスキップされていることを確認
docker compose exec db psql -U vector -d vector -c \
  "SELECT COUNT(*) FROM news_articles WHERE guid LIKE 'hn:%';"
```

---

## 影響サマリ

| 変更対象 | ファイル数 | 破壊的変更 |
|---|---|---|
| Services | 2 (hacker_news.py[NEW], news_fetcher.py) | NO |
| Config | 1 (config.py) | NO |
| Tests | 2 (test_hacker_news.py[NEW], conftest.py) | N/A |
| **合計** | **5ファイル** | |
