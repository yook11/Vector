> **Status**: Done (2026-04-10). Implemented in PR #31 (`review/news-sources-router` branch).
> Superseded by [`admin-router-restructure.md`](../admin-router-restructure.md)
> which generalized the admin-only auth to all admin endpoints.

# NewsSource 一覧エンドポイントの役割分離

## 前提

`GET /api/v1/sources` (= `list_sources`) が **管理 UI のソース一覧** と
**記事一覧画面のフィルタドロップダウン** という、性質の異なる 2 用途で共有されている。

関連: [`schema-router-review/news_source.md`](../schema-router-review/news_source.md) は
スキーマ層レビューとして同じ問題を指摘済み。本 backlog はその「実装方針確定」のためのもの。

## 現状

```
GET /api/v1/sources
  認可:  get_current_user (= 一般ユーザーOK)
  返却: NewsSourceDetailList { items: NewsSourceDetail[], total }
        NewsSourceDetail = id, name, source_type, site_url,
                           endpoint_url, is_active, created_at, updated_at
```

呼び出し元 (どちらも SSR `getSources()` 経由):

| 画面 | コンポーネント | 実際に使うフィールド |
|---|---|---|
| `/settings` | `SourceManager` → `SourceTable` | `id, name, sourceType, endpointUrl, isActive` |
| `/` (記事一覧) | `NewsFilters` | `name` のみ（`id` も非使用） |

## 問題点

### 1. 認証境界の不一致（情報露出寄り）

`GET /sources` は管理リソースなのに `get_current_user` で誰でも叩ける。
レスポンスには `endpoint_url` (= スクレイピング先 URL) や `is_active` 等、
管理者しか知る必要のない情報が含まれている。

CRUD 系 (`POST/DELETE/PATCH`) は `get_admin_user` に揃っているのに、
読み取りだけ穴が空いている状態。

### 2. 過剰フェッチ（フィルタ用途）

`NewsFilters` がドロップダウン描画に必要なのは name のみ
([NewsFilters.tsx:68-72](../../frontend/src/components/news/NewsFilters.tsx#L68-L72)):

```tsx
{[...new Map(sources.map((s) => [s.name, s])).values()].map((src) => (
  <SelectItem key={src.name} value={src.name}>{src.name}</SelectItem>
))}
```

そして記事フィルタ API も `source: SourceName | None`
([articles.py:54](../../backend/app/schemas/articles.py#L54), name ベース)。
**id は誰にも要求されていない**にもかかわらず、8 フィールドの Detail を返している。

### 3. フロント側で name 重複排除している（責務漏れ）

上記 `[...new Map(sources.map((s) => [s.name, s])).values()]` は
データ層が distinct を提供していないことの patch。
本来 API 側が「フィルタ選択肢」として返すなら distinct 済みであるべき。

### 4. NewsSourceEmbed が宙に浮いている

[`embeds.py:26`](../../backend/app/schemas/embeds.py#L26) に既に存在:

```python
class NewsSourceEmbed(_CamelBase):
    """ニュースソースの基本参照情報（フィルタ・表示用）"""
    name: SourceName
```

- `name` のみ
- docstring も「フィルタ・表示用」とフィルタ用途を明示
- `NewsBrief.source` / `NewsDetail.source` の埋め込みでは既に使用されている

しかしフィルタ用途では未使用で、代わりに `NewsSourceDetail` が使われている。

### 5. 型レベルのミスマッチが TS 構造的サブタイピングで隠れている

[settings/page.tsx:21](../../frontend/src/app/(protected)/page.tsx#L117) 周辺:
```tsx
const sourcesData = await getSources();  // NewsSourceDetailList
<NewsFilters sources={sourcesData.items} />  // 期待: NewsSourceEmbed[]
```

`NewsSourceDetail` は `NewsSourceEmbed` の super-set なので TS は通る。
しかし「Detail 型を Embed を期待する関数に渡す」のは構造的には逆向きで、
本来は **Embed を返す専用エンドポイントから取得すべき**。

## あるべき姿（候補）

| 案 | 管理 (`GET /sources`) | フィルタ取得 |
|---|---|---|
| **A. リソース分割** | `get_admin_user` + `NewsSourceDetailList` のまま | 新規 `GET /articles/source-options` を追加。記事ドメイン側の派生として `NewsSourceEmbed[]` を返す |
| **B. 同リソース minimal projection** | `get_admin_user` + Detail | 新規 `GET /sources/embed` (またはクエリパラメータ `?projection=embed`) を追加 |
| **C. distinct 導出** | `get_admin_user` + Detail | フィルタ用途は `articles` 側で `SELECT DISTINCT source.name` で導出 |

### 評価軸

- **境界の素直さ**: A > B > C (フィルタは記事ドメインの派生情報)
- **実装コスト**: B < A < C
- **将来の柔軟性**: B (projection で拡張) > A > C
- **YAGNI**: A (今必要な分だけ) > B > C
- **active=false の扱い**: A/B は `is_active=true` フィルタでシンプル、C は「非active だが過去記事に紐付くソース」の扱いが複雑

## スコープ外

- `NewsSourceEmbed` に `id` を追加するか — 現状の用途では不要 (name ベース)
- `SourceManager` 側の `NewsSourceDetail` フィールド削減 — 管理画面で将来表示する余地があるため Detail に残す方が妥当
- ソートやページング — フィルタ用途では全件取得で十分なソース数を想定

## 検証コマンド

```bash
cd backend
uv run ruff check app/routers/news_sources.py app/services/news_source.py
uv run pytest tests/test_routers/test_news_sources.py -x -q
```

フロント:
```bash
cd frontend
npm run generate-types  # スキーマ変更後
npx tsc --noEmit
```

---

## 決定 (2026-04-10)

**ソースフィルタは削除する + `GET /sources` を admin 限定にする**。

候補 A/B/C のどれでもない第 4 の道。理由:

1. **N=1 の用途**: 「全ソース名一覧 (一般ユーザー向け)」を欲する場面は記事一覧画面のフィルタ 1 箇所のみ。それ以外で必要とする UI も将来計画もない (調査済み: NewsCard / NewsDetail は `NewsBrief.source.name` で完結、Settings は管理 Detail を必要とする別ニーズ)
2. **YAGNI**: 1 用途のために専用エンドポイント (案A) or レスポンス同梱 (案2) はオーバーエンジニアリング
3. **責務の純度**: フィルタを削れば `/sources` は管理リソースに専念でき、認証境界も `get_admin_user` に揃う

**`SourceName` VO は保持する**。`NewsSourceEmbed.name` / `NewsSourceCreate.name` / `NewsSourceDetail.name` で引き続き使用される。削除するのは `ArticleListParams.source` / `SemanticSearchParams.source` の query 経由参照のみ。

## 実装計画 (3 コミット)

### Commit 1: refactor(frontend): remove source filter from dashboard

依存性の起点。先に FE から source の使用を断ち切ることで、後続の BE 変更が安全になる。

**変更:**

[`frontend/src/components/news/NewsFilters.tsx`](../../frontend/src/components/news/NewsFilters.tsx):
- L12: `import type { NewsSourceEmbed } from "@/types"` 削除
- L14-16: `sources?: NewsSourceEmbed[]` prop 削除、`NewsFiltersProps` を空 interface に
- L56-77: Source select ブロック (`{sources && sources.length > 0 && ...}`) を削除

[`frontend/src/app/(protected)/page.tsx`](../../frontend/src/app/(protected)/page.tsx):
- L11: `getSources` import 削除
- L32: `filters` 型から `source?: string` 削除
- L54-55: `parseCommonFilters` の `source` 解析削除
- L81-85: `Promise.all` から `getSources()` を除去 → `[newsData, categoriesData]` の 2-tuple に
- L117: `<NewsFilters sources={sourcesData.items} />` → `<NewsFilters />`

**注:** `getSources()` 関数自体は `lib/api-client.ts:160` に残す。settings ページが引き続き利用する。

**検証:**
```bash
cd frontend
npx biome check src/
npx tsc --noEmit
```

### Commit 2: chore(backend): tighten /sources GET to admin only

Commit 1 が完了して Dashboard が `/sources` を叩かなくなった後、認証境界を締める。

**変更:**

[`backend/app/routers/news_sources.py`](../../backend/app/routers/news_sources.py):
- L25 `list_sources` の `_user: Annotated[CurrentUser, Depends(get_current_user)]` を `Depends(get_admin_user)` に変更

**テスト変更:**

[`backend/tests/test_routers/test_news_sources.py`](../../backend/tests/test_routers/test_news_sources.py):
- L10-17 `test_list_sources_empty`: fixture を `authed_client` → `admin_client`
- L20-39 `test_list_sources`: 同上
- 新規追加: `test_list_sources_forbidden_for_non_admin` — `authed_client` で `GET /api/v1/sources` を叩いて 403 を期待

**検証:**
```bash
cd backend
uv run ruff check app/routers/news_sources.py
uv run pytest tests/test_routers/test_news_sources.py -x -q
```

### Commit 3: refactor(backend): drop unused source filter from articles api

最後に query param と関連クエリロジック・テストを削除。Commit 1 後の FE はすでに送信していないので破壊的影響なし。

**変更 (schemas):**

[`backend/app/schemas/articles.py`](../../backend/app/schemas/articles.py):
- L17: `from app.domain.news_source import SourceName` 削除 (このファイル内で他に使用なし)
- L54: `ArticleListParams.source: Annotated[SourceName | None, Query()] = None` 削除
- L71: `SemanticSearchParams.source: Annotated[SourceName | None, Query()] = None` 削除

**変更 (repositories):**

[`backend/app/repositories/articles.py`](../../backend/app/repositories/articles.py):
- L12: `from app.models.news_source import NewsSource` 削除
- L59-61: `if query.source is not None: source_ids = ...; stmt = stmt.where(...)` ブロック削除

[`backend/app/repositories/semantic_search.py`](../../backend/app/repositories/semantic_search.py):
- L14: `from app.models.news_source import NewsSource` 削除
- L43-45: `if query.source is not None: ...` ブロック削除

**変更なし (services):**
`services/articles.py` と `services/semantic_search.py` は `query` を repo に透過的に渡しているだけなので、メソッドシグネチャ含め変更不要。

**テスト変更:**

[`backend/tests/test_routers/test_articles.py`](../../backend/tests/test_routers/test_articles.py):
- L213-244 `test_filter_by_source_name` 削除
- L245-260 `test_filter_by_source_name_nonexistent` 削除
- L316-323 `test_invalid_source_name_returns_422` 削除 (`SourceName` VO 自体は保持されるが、`source` query 経由の検証は不要)

[`backend/tests/test_semantic_search.py`](../../backend/tests/test_semantic_search.py):
- L153-194 `test_semantic_search_combined_with_source_filter` 削除

**変更 (frontend - 型再生成のみ):**
```bash
cd frontend && npm run generate-types
```
- `src/types/generated.ts` から `source` query param 関連の定義が消える
- Commit 1 で FE 側は既に source を参照していないので、再生成だけで TS error は出ない想定

**検証:**
```bash
cd backend
uv run ruff check app/schemas/articles.py app/repositories/articles.py app/repositories/semantic_search.py
uv run ruff format --check app/schemas/articles.py app/repositories/articles.py app/repositories/semantic_search.py
uv run pytest tests/test_routers/test_articles.py tests/test_semantic_search.py tests/test_routers/test_news_sources.py -x -q

cd ../frontend
npm run generate-types
npx biome check src/types/
npx tsc --noEmit
```

## コミット順序の根拠

各 commit 後で動作可能な状態を保つため:

| 完了時点 | BE state | FE state | 整合性 |
|---|---|---|---|
| Commit 1 後 | source query 受付可、`/sources` 一般可 | source UI なし、URL param 送信なし | OK (BE は orphan param 受付するが害なし) |
| Commit 2 後 | `/sources` admin only | (変更なし) | OK (Dashboard は Commit 1 で `getSources()` を呼ばなくなっている。settings は admin として呼ぶ) |
| Commit 3 後 | source query param なし | 型再生成済み、source 参照なし | クリーン |

逆順 (Commit 2 を先) にすると、Commit 1 が入る前に Dashboard の `getSources().catch(() => ({ items: [], total: 0 }))` が 403 をキャッチして filter dropdown が黙って消える状態が発生する。動作上は壊れないが、ユーザー視認の UI 変化が中途半端なタイミングで起きるので避ける。
